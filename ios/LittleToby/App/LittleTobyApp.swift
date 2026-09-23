import SwiftUI

@main
struct LittleTobyApp: App {
    @State private var model: AppModel
    @AppStorage("toby.appearance") private var appearance = "system"

    init() {
        UITestSupport.prepare()
        _model = State(initialValue: AppModel())
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .preferredColorScheme(colorScheme)
        }
        .backgroundTask(.appRefresh(Notifier.refreshID)) {
            await Notifier.shared.checkInBackground()
        }
    }

    private var colorScheme: ColorScheme? {
        if let forced = UITestSupport.colorScheme { return forced }
        switch appearance {
        case "light": return .light
        case "dark": return .dark
        default: return nil
        }
    }
}
