# Fish-shell env loader for linux-agent.
# Usage: source load-env.fish   (run from inside the linux-agent directory)

if not test -f .env
    echo "No .env found — copy .env.example to .env first."
else
    for line in (grep -v '^#' .env | grep .)
        set parts (string split -m 1 '=' $line)
        if test -n "$parts[2]"
            set -gx $parts[1] $parts[2]
        end
    end
    echo "Environment loaded from .env"
end
