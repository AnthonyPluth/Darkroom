import AuthenticationServices
import DarkroomCore
import SwiftUI

struct SignInView: View {
    @ObservedObject var model: AppModel
    @State private var web = WebSignIn()
    @State private var address = ""
    @State private var deviceName = ""
    @State private var starting = false
    @FocusState private var focus: Field?
    @AccessibilityFocusState private var bannerFocused: Bool

    private enum Field {
        case address
        case name
    }

    private var signingIn: Bool {
        if case .signingIn = model.phase { return true }
        return false
    }

    private var addressError: String? {
        model.addressProblem.map(SignInMessages.address)
    }

    private var nameError: String? {
        model.nameMissing ? SignInMessages.nameMissing : nil
    }

    var body: some View {
        Screen {
            HStack(spacing: 8) {
                Circle().fill(Color.accent).frame(width: 12, height: 12).accessibilityHidden(true)
                Text("Darkroom").font(.title3.bold()).foregroundStyle(Color.ink)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Sign in")
                    .font(.title.bold())
                    .foregroundStyle(Color.ink)
                    .accessibilityAddTraits(.isHeader)
                Text("Darkroom uploads this iPhone's photos to your own server at home.")
                    .font(.subheadline)
                    .foregroundStyle(Color.inkMuted)
            }
            if let banner = model.banner {
                bannerCard(banner)
            }
            field(
                label: "Darkroom address",
                helper: SignInMessages.addressHelper,
                error: addressError
            ) {
                TextField("darkroom.example.com", text: $address)
                    .font(.body.monospaced())
                    .keyboardType(.URL)
                    .textContentType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.go)
                    .focused($focus, equals: .address)
            }
            field(
                label: "Name of this iPhone",
                helper: SignInMessages.nameHelper,
                error: nameError
            ) {
                TextField("Sam's iPhone", text: $deviceName)
                    .font(.body)
                    .textContentType(.name)
                    .textInputAutocapitalization(.words)
                    .submitLabel(.go)
                    .focused($focus, equals: .name)
            }
            PrimaryButton(action: submit) {
                if signingIn {
                    HStack(spacing: 8) {
                        ProgressView().tint(Color.accentInk)
                        Text("Waiting for sign-in")
                    }
                } else {
                    Text("Continue with Pocket ID")
                }
            }
            .disabled(signingIn || starting)
        }
        .onAppear {
            if address.isEmpty, let saved = model.savedServer {
                address = String(saved.text.dropFirst("https://".count))
            }
            if deviceName.isEmpty {
                deviceName = model.suggestedDeviceName()
            }
        }
        .onChange(of: model.banner) { _, new in
            bannerFocused = new != nil
        }
        .onChange(of: addressError) { _, new in announce(new) }
        .onChange(of: nameError) { _, new in announce(new) }
    }

    private func bannerCard(_ banner: SignInBanner) -> some View {
        let failure: Bool
        let message: String
        switch banner {
        case .failure(let text):
            failure = true
            message = text
        case .notice(let text):
            failure = false
            message = text
        }
        return Card(border: failure ? .accent : .line) {
            StatusChip(tone: failure ? .danger : .neutral, label: failure ? "Couldn't sign in" : "Signed out")
            Text(message).font(.subheadline).foregroundStyle(Color.ink)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
        .accessibilityFocused($bannerFocused)
    }

    private func field<Input: View>(
        label: String,
        helper: String,
        error: String?,
        @ViewBuilder input: () -> Input
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(label)
            input()
                .foregroundStyle(Color.ink)
                .tint(Color.accent)
                .padding(.horizontal, 12)
                .frame(minHeight: 48)
                .background(Color.surface, in: RoundedRectangle(cornerRadius: Layout.controlRadius, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: Layout.controlRadius, style: .continuous)
                        .stroke(error == nil ? Color.lineStrong : Color.danger, lineWidth: 1)
                )
                .disabled(signingIn)
                .accessibilityLabel(label)
                .accessibilityHint(error ?? helper)
                .onSubmit(submit)
            Text(error ?? helper)
                .font(.footnote)
                .foregroundStyle(error == nil ? Color.inkMuted : Color.danger)
                .accessibilityHidden(true)
        }
    }

    private func announce(_ text: String?) {
        guard let text else { return }
        AccessibilityNotification.Announcement(text).post()
    }

    private func submit() {
        guard !signingIn, !starting else { return }
        starting = true
        focus = nil
        Task {
            await signIn()
            starting = false
        }
    }

    private func signIn() async {
        guard let start = await model.begin(addressText: address, deviceName: deviceName) else { return }
        do {
            let callback = try await web.authenticate(
                using: start,
                callbackURLScheme: DeviceSignIn.callbackScheme
            )
            await model.finish(callback: callback)
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            await model.cancelled()
        } catch {
            await model.sheetFailed()
        }
    }
}
