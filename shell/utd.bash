# utd: shell shortcut for ultra-todo. Source this file from ~/.bashrc:
#   source /path/to/ultra-todo/shell/utd.bash
#
#   utd REPO [ls | board | next | new | pull | save | migrate | serve | ID [action]]
#   utd ls | utd open NAME|N [flags] | utd forget NAME|N,...
# REPO is . (current repo), a path, or a known repo's name or `utd ls` number. `utd -h` lists everything.

_UTD_PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

utd() {
    ULTRA_TODO_PROG=utd uv run --quiet --project "$_UTD_PROJECT" ultra-todo "$@"
}

# tab completion: commands / repo names / directories, then per-repo actions, ticket ids and values
_utd() {
    local cur="${COMP_WORDS[COMP_CWORD]}" prev="${COMP_WORDS[COMP_CWORD-1]}"
    local first="${COMP_WORDS[1]}" second="${COMP_WORDS[2]}"
    local statuses="todo doing done blocked" prios="high medium low"
    local serve_flags="--lang --host --port --auto-commit --author --max-attachment-mb --db --threads --debug -h --help"
    local filter_flags="--status --priority --assignee -t --tag --any -q --query --open --mine -h --help"
    case "$prev" in
        --lang) COMPREPLY=($(compgen -W "en he" -- "$cur")); return ;;
        --db) compopt -o filenames; COMPREPLY=($(compgen -f -- "$cur")); return ;;
        --host|--port|--author|--max-attachment-mb|--threads|--assignee|-q|--query|-t|--tag|-m|--message|-a|-d|--description|--id) return ;;
        --status) COMPREPLY=($(compgen -W "$statuses" -- "$cur")); return ;;
        -s) COMPREPLY=($(compgen -W "$statuses" -- "$cur")); return ;;
        --priority|-p) COMPREPLY=($(compgen -W "$prios" -- "$cur")); return ;;
    esac
    # first word: commands, repo names, directories
    if [ "$COMP_CWORD" -eq 1 ]; then
        compopt -o filenames 2>/dev/null
        COMPREPLY=($(compgen -W "ls open forget . $(utd ls --names 2>/dev/null)" -- "$cur"))
        [ -n "$cur" ] && mapfile -t -O "${#COMPREPLY[@]}" COMPREPLY < <(compgen -d -- "$cur")
        return
    fi
    case "$first" in
        ls) return ;;
        open) [ "$COMP_CWORD" -eq 2 ] && [[ "$cur" != -* ]] && COMPREPLY=($(compgen -W "$(utd ls --names 2>/dev/null)" -- "$cur")) \
                  || COMPREPLY=($(compgen -W "$serve_flags" -- "$cur")); return ;;
        forget)
            local pre="" name
            [[ "$cur" == *,* ]] && pre="${cur%,*}," && cur="${cur##*,}"
            COMPREPLY=($(for name in $(utd ls --names --all 2>/dev/null); do
                [[ "$name" == "$cur"* ]] && echo "$pre$name"; done)); return ;;
    esac
    # utd REPO <action | ticket id>
    local ids
    ids() { utd "$first" ls --ids 2>/dev/null; }
    if [ "$COMP_CWORD" -eq 2 ]; then
        COMPREPLY=($(compgen -W "ls board next new pull save migrate serve $(ids)" -- "$cur")); return
    fi
    case "$second" in
        ls|board) COMPREPLY=($(compgen -W "$filter_flags --json" -- "$cur")) ;;
        next) COMPREPLY=($(compgen -W "$filter_flags -n --json" -- "$cur")) ;;
        new) COMPREPLY=($(compgen -W "-p --priority -a --assignee -t --tag -d --description --status --id --attach -h" -- "$cur")) ;;
        migrate) COMPREPLY=($(compgen -W "-h" -- "$cur")) ;;
        pull) COMPREPLY=($(compgen -W "--rebase -h" -- "$cur")) ;;
        save) COMPREPLY=($(compgen -W "-m --message --set-upstream -h" -- "$cur")) ;;
        serve) COMPREPLY=($(compgen -W "$serve_flags" -- "$cur")) ;;
        *)  # utd REPO ID ...
            case "$prev" in
                status) COMPREPLY=($(compgen -W "$statuses" -- "$cur")); return ;;
                priority) COMPREPLY=($(compgen -W "$prios" -- "$cur")); return ;;
                blocked|block-by|-b|-bb|-rb|-rbb|--blocked|--block-by|--remove-blocked|--remove-block-by)
                    COMPREPLY=($(compgen -W "$(ids)" -- "$cur")); return ;;
                remove) COMPREPLY=($(compgen -W "blocked block-by" -- "$cur")); return ;;
                attach|--attach) compopt -o filenames 2>/dev/null; COMPREPLY=($(compgen -f -- "$cur")); return ;;
            esac
            if [ "$COMP_CWORD" -eq 3 ] && [[ "$cur" != -* ]]; then
                COMPREPLY=($(compgen -W "edit comment status start done reopen priority attach detach open blocked block-by remove" -- "$cur"))
            else
                COMPREPLY=($(compgen -W "-e -c -m -s -p -a -d -o --path -b -bb -rb -rbb --json -h" -- "$cur"))
            fi ;;
    esac
}
complete -F _utd utd
