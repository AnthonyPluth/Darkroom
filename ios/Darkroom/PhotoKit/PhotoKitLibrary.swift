import DarkroomCore
import Photos

struct PhotoKitLibrary: PhotoLibrary {
    static func requireAccess() async throws {
        var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if status == .notDetermined {
            status = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        }
        guard status == .authorized else { throw PhotoLibraryError.accessDenied }
    }

    static func fetchOptions() -> PHFetchOptions {
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: true)]
        options.includeHiddenAssets = true
        options.includeAllBurstAssets = true
        options.includeAssetSourceTypes = [.typeUserLibrary]
        return options
    }

    static func assetResource(for resource: PhotoResource) -> PHAssetResource? {
        let found = PHAsset.fetchAssets(withLocalIdentifiers: [resource.assetID], options: fetchOptions())
        guard let asset = found.firstObject else { return nil }
        let sameKind = PHAssetResource.assetResources(for: asset).filter { $0.type.darkroomKind == resource.kind }
        return sameKind.first { $0.originalFilename == resource.originalFilename } ?? sameKind.first
    }

    func allResources() async throws -> [PhotoResource] {
        try await Self.requireAccess()
        let assets = PHAsset.fetchAssets(with: Self.fetchOptions())
        var found: [PhotoResource] = []
        assets.enumerateObjects { asset, _, stop in
            if Task.isCancelled {
                stop.pointee = true
                return
            }
            found.append(contentsOf: Self.resources(of: asset))
        }
        try Task.checkCancellation()
        return found
    }

    func resources(ofAsset assetID: String) async throws -> [PhotoResource] {
        try await Self.requireAccess()
        let assets = PHAsset.fetchAssets(withLocalIdentifiers: [assetID], options: Self.fetchOptions())
        guard let asset = assets.firstObject else { return [] }
        return Self.resources(of: asset)
    }

    private static func resources(of asset: PHAsset) -> [PhotoResource] {
        let changed = asset.modificationDate ?? asset.creationDate
        let stamp = changed.map { String(Int64($0.timeIntervalSince1970 * 1000)) } ?? "0"
        var kinds: Set<ResourceKind> = []
        var found: [PhotoResource] = []
        for resource in PHAssetResource.assetResources(for: asset) {
            guard let kind = resource.type.darkroomKind, kinds.insert(kind).inserted else { continue }
            found.append(PhotoResource(
                assetID: asset.localIdentifier,
                kind: kind,
                originalFilename: resource.originalFilename,
                version: "\(stamp)|\(resource.type.rawValue)|\(resource.originalFilename)"
            ))
        }
        return found
    }
}

extension PHAssetResourceType {
    var darkroomKind: ResourceKind? {
        switch self {
        case .photo, .video: return .photo
        case .pairedVideo: return .liveVideo
        case .fullSizePhoto, .fullSizeVideo: return .adjusted
        default: return nil
        }
    }
}

struct PhotoKitExporter: ResourceExporter {
    func export(_ resource: PhotoResource, to destination: URL) async throws -> ExportOutcome {
        guard let target = PhotoKitLibrary.assetResource(for: resource) else { throw ExportError.resourceGone }
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = false
        do {
            try await PHAssetResourceManager.default().writeData(for: target, toFile: destination, options: options)
            return .exported
        } catch {
            if Task.isCancelled { throw CancellationError() }
            let failure = error as NSError
            if failure.domain == PHPhotosErrorDomain, failure.code == PHPhotosError.Code.networkAccessRequired.rawValue {
                return .notLocal
            }
            throw error
        }
    }
}
