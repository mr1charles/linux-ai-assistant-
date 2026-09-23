import Foundation

// The JSON the computer's Toby sends (scripts/remote_bridge.py and
// RemoteServices in scripts/linux_agent_apple.py). Keys arrive in
// snake_case and are decoded with .convertFromSnakeCase. Almost everything
// is optional: a value the computer couldn't measure is absent or null, and
// the app shows it as unknown rather than inventing one.

struct ComputerInfo: Codable, Hashable {
    var id: String
    var name: String
    var api: Int?
}

struct DeviceInfo: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var platform: String?
    var pairedAt: Double?
    var lastSeen: Double?
    var thisDevice: Bool?
}

struct TaskStep: Codable, Hashable {
    var label: String
    var status: String          // pending | current | done | error
    var detail: String?
}

/// The states a task can be in, as the computer reports them.
enum TaskState: String, Codable {
    case thinking, preparing, working, waiting
    case needsPermission = "needs_permission"
    case paused, completed, failed, cancelled

    var label: String {
        switch self {
        case .thinking: return "Thinking"
        case .preparing: return "Getting ready"
        case .working: return "Working"
        case .waiting: return "Waiting"
        case .needsPermission: return "Needs your OK"
        case .paused: return "Paused"
        case .completed: return "Done"
        case .failed: return "Didn't finish"
        case .cancelled: return "Stopped"
        }
    }

    var isFinished: Bool { self == .completed || self == .failed || self == .cancelled }
}

struct FileTouch: Codable, Hashable {
    var path: String
    var action: String
}

struct JobInfo: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var command: String?
    var cwd: String?
    var state: String           // running | paused | succeeded | failed | stopped
    var progress: Double?
    var started: Double?
    var ended: Double?
    var exitCode: Int?
    var lastLine: String?
    var watched: Bool?
    var output: [String]?

    var isRunning: Bool { state == "running" || state == "paused" }
}

struct TaskInfo: Codable, Hashable, Identifiable {
    var id: String
    var text: String
    var origin: String?
    var state: String
    var created: Double?
    var ended: Double?
    var error: String?
    var steps: [TaskStep]?
    var reply: String?
    var files: [FileTouch]?
    var jobs: [String]?
    var jobDetails: [JobInfo]?
    var stepsDone: Int?
    var stepsTotal: Int?

    var taskState: TaskState? { TaskState(rawValue: state) }
    var fromPhone: Bool { origin?.hasPrefix("phone") ?? false }
}

struct Approval: Codable, Hashable, Identifiable {
    var id: String
    var kind: String?
    var level: String           // confirm | restricted
    var title: String
    var details: [String]?
    var reason: String?
    var created: Double?
    var expires: Double?

    var isRestricted: Bool { level == "restricted" }
}

struct HistoryMessage: Codable, Hashable {
    var role: String
    var content: String
}

/// The live snapshot, long-polled from /api/state.
struct TobyState: Codable {
    var version: Int
    var busy: Bool?
    var task: TaskInfo?
    var reply: String?
    var approvals: [Approval]?
    var history: [HistoryMessage]?
    var jobs: [JobInfo]?
    var workMode: Bool?
    var power: String?
    var screenView: Bool?
    var viewing: Bool?
    var computer: ComputerInfo?
    var device: DeviceInfo?
    var eventsLast: Int?
}

struct Usage: Codable, Hashable {
    var total: Int64?
    var used: Int64?
    var percent: Double?
}

struct Battery: Codable, Hashable {
    var percent: Int?
    var status: String?
    var charging: Bool?
}

struct TobyInfo: Codable, Hashable {
    var busy: Bool?
    var model: String?
    var power: String?
    var currentTask: String?
    var workMode: Bool?
}

struct Policy: Codable, Hashable {
    var screenView: Bool?
    var restrictedActions: Bool?
}

/// /api/status: real measurements of the computer.
struct SystemStatus: Codable, Hashable {
    var hostname: String?
    var os: String?
    var kernel: String?
    var uptimeS: Int?
    var cpuPercent: Double?
    var memory: Usage?
    var battery: Battery?
    var disk: Usage?
    var load: [Double]?
    var sampledAt: Double?
    var toby: TobyInfo?
    var policy: Policy?
    var jobsRunning: Int?
    var computer: ComputerInfo?
}

struct WindowInfo: Codable, Hashable, Identifiable {
    var address: String
    var title: String
    var app: String
    var workspace: String?
    var pid: Int?
    var focused: Bool?
    var fullscreen: Bool?

    var id: String { address }
}

struct ScreenSize: Codable, Hashable {
    var width: Int
    var height: Int
}

struct Overview: Codable {
    var windows: [WindowInfo]
    var screen: ScreenSize?
    var screenView: Bool?
}

struct TobyEvent: Codable, Hashable, Identifiable {
    var id: Int
    var kind: String
    var title: String
    var body: String
    var at: Double
}

// -- responses ------------------------------------------------------------------

struct EventsResponse: Codable { var events: [TobyEvent]; var last: Int }
struct TasksResponse: Codable { var tasks: [TaskInfo] }
struct JobsResponse: Codable { var jobs: [JobInfo] }
struct FilesResponse: Codable { var files: [FileTouch] }
struct DevicesResponse: Codable { var devices: [DeviceInfo] }
struct AskResponse: Codable { var ok: Bool; var message: String?; var taskId: String? }
struct OKResponse: Codable { var ok: Bool; var message: String? }
struct HelloResponse: Codable { var app: String; var id: String; var name: String; var api: Int? }
struct ClaimResponse: Codable { var claim: String; var compare: String; var computer: ComputerInfo; var expiresIn: Int? }
struct PairWaitResponse: Codable { var status: String; var compare: String?; var token: String?; var device: DeviceInfo? }
struct APIErrorBody: Codable { var error: String?; var code: String? }

enum JSON {
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()
}
