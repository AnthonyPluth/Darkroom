import DarkroomCore
import Foundation

final class BackgroundUploadTransport: NSObject, UploadTransport, URLSessionTaskDelegate, @unchecked Sendable {
    private let client: APIClient
    private let library: PhotoLibrary
    private let exporter: ResourceExporter
    private let directory: URL
    private let identifier: String
    private let lock = NSLock()
    private var waiting: [Int: CheckedContinuation<Void, Error>] = [:]
    private var files: [Int: URL] = [:]
    private var session: URLSession!

    init(client: APIClient, library: PhotoLibrary, exporter: ResourceExporter, identifier: String, directory: URL) {
        self.client = client
        self.library = library
        self.exporter = exporter
        self.identifier = identifier
        self.directory = directory
        super.init()
        try? FileManager.default.removeItem(at: directory)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let configuration = URLSessionConfiguration.background(withIdentifier: identifier)
        configuration.sessionSendsLaunchEvents = true
        configuration.isDiscretionary = false
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        session.getAllTasks { tasks in
            for task in tasks { task.cancel() }
        }
    }

    func cancelAll() {
        session.invalidateAndCancel()
    }

    func upload(_ resource: LocalResource) async throws {
        let file = directory.appendingPathComponent(UUID().uuidString)
        do {
            let source = try await find(resource)
            let outcome: ExportOutcome
            do {
                outcome = try await exporter.export(source, to: file)
            } catch {
                throw APIError.forLocalExport(error)
            }
            guard outcome == .exported else {
                throw APIError.rejected(status: 422)
            }
            let request = try await client.uploadRequest(
                assetID: resource.key.assetID, kind: resource.key.kind, sha1: resource.sha1, filename: resource.filename
            )
            try await send(request, file: file)
        } catch {
            try? FileManager.default.removeItem(at: file)
            throw error
        }
    }

    private func find(_ resource: LocalResource) async throws -> PhotoResource {
        let resources: [PhotoResource]
        do {
            resources = try await library.resources(ofAsset: resource.key.assetID)
        } catch {
            throw APIError.forLocalExport(error is PhotoLibraryError ? ExportError.resourceGone : error)
        }
        guard let source = resources.first(where: { $0.key == resource.key }) else {
            throw APIError.rejected(status: 409)
        }
        return source
    }

    private func send(_ request: URLRequest, file: URL) async throws {
        let task = session.uploadTask(with: request, fromFile: file)
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                lock.lock()
                waiting[task.taskIdentifier] = continuation
                files[task.taskIdentifier] = file
                lock.unlock()
                task.resume()
            }
        } onCancel: {
            task.cancel()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let continuation = waiting.removeValue(forKey: task.taskIdentifier)
        let file = files.removeValue(forKey: task.taskIdentifier)
        lock.unlock()
        if let file { try? FileManager.default.removeItem(at: file) }
        guard let continuation else { return }
        if let error {
            let code = (error as? URLError)?.code
            if code == .cancelled {
                continuation.resume(throwing: CancellationError())
            } else {
                continuation.resume(throwing: APIError.transport(String(code?.rawValue ?? -1)))
            }
            return
        }
        let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        if let failure = APIError.forUpload(status: status) {
            continuation.resume(throwing: failure)
        } else {
            continuation.resume()
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        BackgroundSessionCompletions.shared.complete(identifier: identifier)
    }
}
