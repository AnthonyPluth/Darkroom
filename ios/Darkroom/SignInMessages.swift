import DarkroomCore

enum SignInMessages {
    static let cancelled = "Sign-in was cancelled."
    static let unreadable = "Couldn't read the sign-in saved on this iPhone. Sign in again."
    static let sheetFailed = "Couldn't open the sign-in page. Try again."
    static let nameMissing = "Name this iPhone so you can find it on the People page."
    static let addressHelper = "The address you open Darkroom at on a computer. Away from home, turn Tailscale on first."
    static let nameHelper = "Shown on the People page so you can tell the family's iPhones apart."

    static func address(_ problem: ServerAddress.Problem) -> String {
        switch problem {
        case .empty: "Enter your Darkroom's address."
        case .notHTTPS: "Darkroom needs an address that starts with https."
        case .invalid: "That doesn't look like an address. Try something like darkroom.example.com."
        }
    }

    /// The banner text for a failed sign-in. `alreadySignedIn`, `notSigningIn` and `server` cannot be reached from the
    /// sign-in screen, and the design gives them no copy, so they show no banner.
    static func failure(_ error: SignInError) -> String? {
        switch error {
        case .noToken: "Darkroom didn't send the sign-in back. Try again."
        case .stateMismatch: "That sign-in didn't match this attempt, so the app ignored it. Try again."
        case .expired: "That sign-in took more than 10 minutes. Try again."
        case .storage: "Couldn't save the sign-in on this iPhone. Try again, and restart the iPhone if it keeps happening."
        case .noPerson:
            "No person with this email. Ask whoever runs Darkroom to add your email to your person on the People page, "
                + "then sign in again."
        case .notAllowed:
            "This account isn't allowed to use the Darkroom app. If it is yours, ask whoever runs Darkroom to add it to "
                + "the family group in Pocket ID, then try again."
        case .alreadySignedIn, .notSigningIn, .server: nil
        }
    }
}
