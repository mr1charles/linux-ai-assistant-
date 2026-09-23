import Foundation

enum Format {
    /// "just now", "12s ago", "4m ago", "2h ago", or a date.
    static func ago(_ date: Date?, now: Date = Date()) -> String {
        guard let date else { return "never" }
        let seconds = Int(now.timeIntervalSince(date))
        if seconds < 5 { return "just now" }
        if seconds < 60 { return "\(seconds)s ago" }
        if seconds < 3600 { return "\(seconds / 60)m ago" }
        if seconds < 86_400 { return "\(seconds / 3600)h ago" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }

    static func ago(epoch: Double?, now: Date = Date()) -> String {
        guard let epoch else { return "never" }
        return ago(Date(timeIntervalSince1970: epoch), now: now)
    }

    /// "2h 14m", "5m", "40s".
    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds >= 0 else { return "—" }
        let s = Int(seconds)
        let h = s / 3600, m = (s % 3600) / 60
        if h > 0 { return "\(h)h \(m)m" }
        if m > 0 { return "\(m)m" }
        return "\(s)s"
    }

    static func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(value.rounded()))%"
    }

    static func bytes(_ value: Int64?) -> String {
        guard let value else { return "—" }
        return ByteCountFormatter.string(fromByteCount: value, countStyle: .memory)
    }
}
