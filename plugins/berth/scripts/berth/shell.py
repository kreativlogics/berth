"""Split a shell command line into simple commands for the guard rules.

This is a best-effort reader, not a shell. It handles quoting (including $'...'), separators, command substitution
(also inside arithmetic and ${...}), heredocs, scripts fed to a shell on stdin, `sh -c` / `eval`, leading VAR=value
assignments, reserved words (if, then, do, !...), common wrappers (sudo, env, timeout, xargs...) and `cd` inside a
chain. What it can't see (aliases, functions, variables expanded at run time, scripts written and then run) is
listed in the repository's docs/limits.md.
"""

import os
import re

SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", ";&", "(", ")", "{", "}"}
REDIRECT_WRITE = {">", ">>", ">|", "&>", "&>>"}
REDIRECT_OTHER = {"<", "<<", "<<<", "<>", ">&", "<&"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "yash", "posh", "fish", "ash", "busybox", "csh", "tcsh"}
# Wrappers and the options of theirs that take a value.
WRAPPERS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user", "--group", "--close-from",
             "--chdir", "--host", "--prompt", "--role", "--type", "--other-user", "--command-timeout"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-P", "-a", "--unset", "--chdir", "--argv0"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": {"-f", "-o"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
    "caffeinate": {"-t", "-w"},
    "xargs": {"-I", "-J", "-L", "-R", "-S", "-n", "-P", "-s", "-d", "-E", "-a", "--max-args", "--max-procs",
              "--max-lines", "--max-chars", "--delimiter", "--arg-file"},
    "chronic": set(),
    "unbuffer": set(),
    "watch": {"-n", "--interval"},
    "noglob": set(),
    "nocorrect": set(),
    "arch": {"-arch", "-e"},
    "xcrun": {"-sdk", "--sdk", "-toolchain", "--toolchain"},
    "script": set(),  # Read by _script_command: its file comes before the command.
}
# Reserved words that can start a command; the command after them is what runs.
RESERVED = {"!", "if", "then", "else", "elif", "do", "while", "until", "coproc"}
# Paths that make `source` read the script from stdin.
STDIN_PATHS = {"/dev/stdin", "-", "/dev/fd/0", "/proc/self/fd/0"}

# Commands longer than this are not read; the call asks instead.
MAX_COMMAND = 256 * 1024
# Nesting of $(...), `...`, ${...}, arithmetic, sh -c, eval and shell stdin together. Deeper is denied: nothing
# legitimate nests this far, and reading further would run out of stack.
MAX_DEPTH = 64
# Total text read across all nested re-reads (sh -c, eval, backticks, shell stdin) of one call.
MAX_WORK = 4 * 1024 * 1024


class Cmd(object):
    """One simple command after wrappers and assignments are stripped.

    stdin_text is the text of the heredocs (tab-stripped for <<-, not expanded) and here-strings (with the newline
    the shell adds) attached to this command, in order; "" when there are none.
    """

    __slots__ = ("name", "argv", "args", "env", "redirects", "cwd", "depth", "stdin_text")

    def __init__(self, argv, env, redirects, cwd, depth, stdin_text=""):
        self.argv = argv
        self.name = _base(argv[0]) if argv else ""
        self.args = argv[1:]
        self.env = env
        self.redirects = redirects  # list of (operator, target)
        self.cwd = cwd
        self.depth = depth
        self.stdin_text = stdin_text

    def __repr__(self):
        return "Cmd(%r, env=%r, redirects=%r, cwd=%r)" % (self.argv, self.env, self.redirects, self.cwd)


class ParseResult(object):
    def __init__(self):
        self.cmds = []
        self.error = False  # An unclosed quote: the shell would refuse the rest.
        self.problem = None  # Why the command could not be read in full, or None.
        self.problem_level = None  # ask or deny.
        self.env_split = False  # An env -S string could not be read exactly; kept even if a later problem replaces it.
        self.work = 0

    def flag(self, level, reason):
        if self.problem_level != "deny":
            self.problem, self.problem_level = reason, level


class _Limit(Exception):
    """The command nests too deeply or is too much text to read."""


class _Op(str):
    """An unquoted shell operator (a quoted ';' is a plain word)."""


class _HereDoc(str):
    """The word after <<: reads as __HEREDOC__, carries the body."""

    def __new__(cls, delim, strip_tabs, expands):
        self = str.__new__(cls, "__HEREDOC__")
        self.delim = delim
        self.strip_tabs = strip_tabs
        self.expands = expands
        self.body = ""
        return self


_PIPES = {"|", "|&"}
_NO_PIPE = object()  # The command has no pipe into it.
_SHELL_VALUE_OPTIONS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
_UNKNOWN_PIPE = ("The command pipes unknown text into a shell (%s), so checkrein cannot see what it runs. Save it to "
                 "a file and review it first.")
_ENV_SPLIT_REASON = ("The command gives env a split string that cannot be read exactly (a variable, an unclosed "
                     "quote or an unknown escape), so the program it runs is not known.")
_SUBST = "__SUBST__"
_ARITH = "__ARITH__"
_BREAK = frozenset(" \t\n;&|()<>")
_OPERATORS = sorted(SEPARATORS | REDIRECT_WRITE | REDIRECT_OTHER, key=len, reverse=True)
# Runs of characters with no special meaning, consumed in one step so reading stays linear and fast.
_PLAIN = re.compile(r"[^ \t\n;&|()<>'\"\\$`]+")
_DQ_PLAIN = re.compile(r"[^\"\\$`]+")
_ANSI_PLAIN = re.compile(r"[^\\']+")
_BODY_PLAIN = re.compile(r"[^\\$`]+")
_NEST_PLAIN = re.compile(r"[^\\'\"$`(){}\[\]]+")
_BACKTICK_PLAIN = re.compile(r"[^\\`]+")
_ANSI_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                 "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
_OCTAL = (re.compile(r"[0-7]{1,3}"), 8)
_ANSI_NUMBERS = {"x": (re.compile(r"[0-9a-fA-F]{1,2}"), 16), "u": (re.compile(r"[0-9a-fA-F]{1,4}"), 16),
                 "U": (re.compile(r"[0-9a-fA-F]{1,8}"), 16)}
_ANSI_NUMBERS.update((d, _OCTAL) for d in "01234567")
_DEPTH_REASON = ("The command nests substitutions, quotes or shells more than %d levels deep, so checkrein cannot read "
                 "it. Split it into simpler commands." % MAX_DEPTH)
_WORK_REASON = ("The command re-reads more than %d MB of nested shell text, so checkrein cannot read it. Split it "
                "into simpler commands." % (MAX_WORK // (1024 * 1024)))


def _word(parts, quoted):
    text = "".join(parts)
    if not quoted and text in ("{", "}"):
        return _Op(text)
    return text


class _Lexer(object):
    """Reads shell text into tokens in one pass, with every quote, expansion and heredoc state the shell has.

    Command substitutions become the word __SUBST__ and their tokens go to self.inners; arithmetic becomes
    __ARITH__ (its substitutions still go to self.inners).
    """

    def __init__(self, text, result, depth):
        if depth > MAX_DEPTH:
            raise _Limit(_DEPTH_REASON)
        result.work += len(text)
        if result.work > MAX_WORK:
            raise _Limit(_WORK_REASON)
        self.text = text
        self.result = result
        self.depth = depth
        self.nest = 0
        self.inners = []
        self._pairs = None

    def run(self):
        tokens, _end = self.lex(0, len(self.text), False)
        return tokens, self.inners

    def _enter(self):
        self.nest += 1
        if self.depth + self.nest > MAX_DEPTH:
            raise _Limit(_DEPTH_REASON)

    def lex(self, i, end, sub):
        """Tokens from i to end. In a $(...) body (sub), stop just past its closing paren."""
        text = self.text
        tokens = []
        word = []
        in_word = quoted = False
        pending = []  # Heredocs whose bodies start after the next newline.
        depth = 0  # Plain ( ... ) groups inside a $(...) body.
        while i < end:
            c = text[i]
            if c == "\\" and text.startswith("\n", i + 1):
                i += 2  # A line continuation joins the lines, inside a word or between words.
                continue
            if c in _BREAK:
                if in_word:
                    tokens.append(_word(word, quoted))
                    word = []
                    in_word = quoted = False
                if c == " " or c == "\t":
                    i += 1
                    continue
                if c == "\n":
                    tokens.append(_Op(";"))
                    i += 1
                    if pending:
                        i = self._heredoc_bodies(pending, i, end, sub)
                        pending = []
                    continue
                if c in "<>" and text.startswith("(", i + 1):
                    self._enter()
                    tokens.append(_SUBST)
                    i = self._sub(i + 2, end)
                    continue
                if c == "(" and text.startswith("((", i) and self._is_arith(i):
                    tokens.append(_ARITH)
                    i = self._arith(i + 2, end, ")")
                    continue
                if sub and c == ")":
                    if depth == 0:
                        return tokens, i + 1  # Heredocs still pending here have no body in this substitution.
                    depth -= 1
                elif sub and c == "(":
                    depth += 1
                if c == "<" and text.startswith("<<", i) and not text.startswith("<<<", i):
                    strip_tabs = text.startswith("-", i + 2)
                    tokens.append(_Op("<<"))
                    i, doc = self._delimiter(i + 2 + strip_tabs, end, strip_tabs)
                    if doc is not None:
                        tokens.append(doc)
                        pending.append(doc)
                    continue
                op = next(o for o in _OPERATORS if text.startswith(o, i))
                tokens.append(_Op(op))
                i += len(op)
                continue
            if c == "#" and not in_word:
                j = text.find("\n", i, end)
                i = end if j == -1 else j
                continue
            in_word = True
            m = _PLAIN.match(text, i, end)
            if m:
                word.append(m.group())
                i = m.end()
                continue
            i, q = self._piece(i, end, word)
            quoted = quoted or q
        if in_word:
            tokens.append(_word(word, quoted))
        return tokens, end

    def _piece(self, i, end, word):
        """One quoted or expanded part of a word at text[i]. Returns (next index, quoted)."""
        text = self.text
        c = text[i]
        if c == "'":
            j = text.find("'", i + 1, end)
            if j == -1:
                self.result.error = True
                word.append(text[i + 1:end])
                return end, True
            word.append(text[i + 1:j])
            return j + 1, True
        if c == '"':
            return self._dquote(i + 1, end, word), True
        if c == "\\":
            word.append(text[i + 1:i + 2])
            return min(i + 2, end), True
        if c == "$":
            return self._dollar(i, end, word, False)
        if c == "`":
            return self._backtick(i, end, word), False
        word.append(c)
        return i + 1, False

    def _dquote(self, i, end, word):
        """Inside "...": only \\, $ and ` are special. Returns the index past the closing quote."""
        text = self.text
        while i < end:
            m = _DQ_PLAIN.match(text, i, end)
            if m:
                word.append(m.group())
                i = m.end()
                continue
            c = text[i]
            if c == '"':
                return i + 1
            if c == "\\":
                nxt = text[i + 1:i + 2]
                if nxt in ("$", "`", '"', "\\"):
                    word.append(nxt)
                elif nxt != "\n":
                    word.append(c + nxt)
                i += 2
            elif c == "$":
                i, _q = self._dollar(i, end, word, True)
            else:
                i = self._backtick(i, end, word)
        self.result.error = True
        return end

    def _dollar(self, i, end, word, dq):
        """An expansion at text[i] == '$'. Returns (next index, quoted)."""
        text = self.text
        nxt = text[i + 1:i + 2] if i + 1 < end else ""
        if nxt == "(":
            self._enter()
            if text.startswith("((", i + 1) and self._is_arith(i + 1):
                self.nest -= 1
                word.append(_ARITH)
                return self._arith(i + 3, end, ")"), False
            word.append(_SUBST)
            return self._sub(i + 2, end), False
        if nxt == "{":
            j = self._braces(i + 2, end)
            word.append(text[i:j])  # Kept as written, so ${HOME}/... still resolves as a path.
            return j, False
        if nxt == "[":
            word.append(_ARITH)
            return self._arith(i + 2, end, "]"), False
        if nxt == "'" and not dq:
            return self._ansi_c(i + 2, end, word), True
        if nxt == '"' and not dq:
            return i + 1, False  # $"..." is a double-quoted string.
        word.append("$")
        return i + 1, False

    def _sub(self, i, end):
        """A $(...) or <(...) body starting at i; the caller has entered one level."""
        tokens, j = self.lex(i, end, True)
        self.nest -= 1
        self.inners.append(tokens)
        return j

    def _backtick(self, i, end, word):
        """`...`: the body, with \\$, \\` and \\\\ unescaped, is read again as its own command line."""
        text = self.text
        parts = []
        j = i + 1
        while j < end:
            m = _BACKTICK_PLAIN.match(text, j, end)
            if m:
                parts.append(m.group())
                j = m.end()
                continue
            if text[j] == "`":
                break
            nxt = text[j + 1:j + 2]
            parts.append(nxt if nxt in ("$", "`", "\\") else "\\" + nxt)
            j += 2
        else:
            self.result.error = True
        word.append(_SUBST)
        inner = _Lexer("".join(parts), self.result, self.depth + self.nest + 1)
        tokens, inners = inner.run()
        self.inners.append(tokens)
        self.inners.extend(inners)
        return min(j + 1, end)

    def _nested(self, i, end, opener, closer, arith):
        """Skip a ${...}, $((...)) or $[...] body to its closer, reading the substitutions inside.

        Quotes are honored when finding the end, as the shell does; in arithmetic a lone `)` also ends it.
        """
        self._enter()
        text = self.text
        scratch = []
        depth = 0
        while i < end:
            m = _NEST_PLAIN.match(text, i, end)
            if m:
                i = m.end()
                continue
            c = text[i]
            if c == "\\":
                i += 2
            elif c == "'":
                j = text.find("'", i + 1, end)
                i = end if j == -1 else j + 1
            elif c == '"':
                i = self._dquote(i + 1, end, scratch)
            elif c == "$":
                i, _q = self._dollar(i, end, scratch, arith)
            elif c == "`":
                i = self._backtick(i, end, scratch)
            elif c == opener:
                depth += 1
                i += 1
            elif c == closer:
                if depth == 0:
                    self.nest -= 1
                    if arith and closer == ")" and text.startswith(")", i + 1):
                        return i + 2
                    return i + 1
                depth -= 1
                i += 1
            else:
                i += 1
        self.nest -= 1
        return end

    def _braces(self, i, end):
        return self._nested(i, end, "{", "}", False)

    def _arith(self, i, end, closer):
        return self._nested(i, end, "(" if closer == ")" else "[", closer, True)

    def _ansi_c(self, i, end, word):
        """$'...' with C escapes, where \\' does not end the string. Returns the index past the closing quote."""
        text = self.text
        while i < end:
            m = _ANSI_PLAIN.match(text, i, end)
            if m:
                word.append(m.group())
                i = m.end()
                continue
            if text[i] == "'":
                return i + 1
            i = self._ansi_escape(i + 1, end, word)
        self.result.error = True
        return end

    def _ansi_escape(self, i, end, word):
        text = self.text
        c = text[i:i + 1] if i < end else ""
        if c and c in _ANSI_ESCAPES:
            word.append(_ANSI_ESCAPES[c])
            return i + 1
        if c in _ANSI_NUMBERS:
            pattern, base = _ANSI_NUMBERS[c]
            m = pattern.match(text, i if c.isdigit() else i + 1, end)
            if m:
                code = int(m.group(), base)
                word.append(chr(code & 0xFF) if base == 8 else chr(code) if code <= 0x10FFFF else "")
                return m.end()
        if c == "c" and i + 1 < end:
            word.append(chr(ord(text[i + 1]) & 0x1F))
            return i + 2
        word.append("\\" + c)
        return min(i + 1, end)

    def _is_arith(self, i):
        """True when the (( at text[i] closes with )) as one pair, as the shell requires for arithmetic."""
        if self._pairs is None:
            self._pairs = {}
            stack = []
            for m in re.finditer(r"[()]", self.text):
                if m.group() == "(":
                    stack.append(m.start())
                elif stack:
                    self._pairs[stack.pop()] = m.start()
        inner = self._pairs.get(i + 1)
        return inner is not None and self.text.startswith(")", inner + 1) and self._pairs.get(i) == inner + 1

    def _delimiter(self, i, end, strip_tabs):
        """Read the heredoc word after << or <<- as the shell does: one whole word, quotes removed, not expanded.

        Returns (index after it, _HereDoc or None).
        """
        text = self.text
        while i < end and text[i] in " \t":
            i += 1
        parts = []
        quoted = False
        while i < end:
            c = text[i]
            if c in _BREAK:
                break
            if c == "'" or c == '"':
                j = text.find(c, i + 1, end)
                j = end if j == -1 else j
                parts.append(text[i + 1:j])
                quoted = True
                i = j + 1
            elif c == "\\":
                parts.append(text[i + 1:i + 2])
                quoted = True
                i += 2
            elif c == "$" and text.startswith("'", i + 1):
                i = self._ansi_c(i + 2, end, parts)
                quoted = True
            elif c == "$" and text[i + 1:i + 2] in ("(", "{", "["):
                j = self._skip_verbatim(i + 1, end)
                parts.append(text[i:j])
                i = j
            elif c == "`":
                j = text.find("`", i + 1, end)
                j = end if j == -1 else j + 1
                parts.append(text[i:j])
                i = j
            else:
                parts.append(c)
                i += 1
        if not parts:
            return min(i, end), None
        return min(i, end), _HereDoc("".join(parts), strip_tabs, not quoted)

    def _skip_verbatim(self, i, end):
        """Index past the bracket group opening at text[i], counting only brackets (a heredoc word is not run)."""
        text = self.text
        opener = text[i]
        closer = {"(": ")", "{": "}", "[": "]"}[opener]
        depth = 0
        while i < end:
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return end

    def _heredoc_bodies(self, pending, i, end, sub):
        """Read each pending heredoc body from i. Returns the index where reading commands resumes.

        Inside $(...), a line that starts with the delimiter followed by `)` also ends the body (bash reads
        `EOF)` that way), and the `)` then closes the substitution. Ending early is the safe side: anything after
        is read as commands.
        """
        text = self.text
        for doc in pending:
            start = i
            resume = body_end = end
            closes = False
            while i < end:
                j = text.find("\n", i, end)
                line_end = end if j == -1 else j
                line = text[i:line_end]
                cmp = line.lstrip("\t") if doc.strip_tabs else line
                if cmp == doc.delim:
                    body_end, resume = i, (line_end + 1 if j != -1 else end)
                    break
                if sub and cmp.startswith(doc.delim) and cmp[len(doc.delim):].lstrip(" \t").startswith(")"):
                    k = i + len(line) - len(cmp) + len(doc.delim)
                    while text[k] in " \t":
                        k += 1
                    body_end, resume, closes = i, k, True
                    break
                i = line_end + 1 if j != -1 else end
            body = text[start:body_end]
            if doc.strip_tabs:
                body = "".join(line.lstrip("\t") for line in body.splitlines(True))
            doc.body = body
            if doc.expands:
                self._body_substitutions(start, body_end)
            i = resume
            if closes:
                return resume
        return i

    def _body_substitutions(self, i, end):
        """$(...) and `...` in an unquoted heredoc body run when the heredoc is read."""
        text = self.text
        scratch = []
        while i < end:
            m = _BODY_PLAIN.match(text, i, end)
            if m:
                i = m.end()
                continue
            c = text[i]
            if c == "\\":
                i += 2
            elif c == "$":
                i, _q = self._dollar(i, end, scratch, True)
            else:
                i = self._backtick(i, end, scratch)


def _is_assignment(tok):
    if "=" not in tok or tok.startswith("="):
        return False
    name = tok.split("=", 1)[0]
    return name.replace("_", "a").isalnum() and not name[0].isdigit()


def _base(word):
    """A program's name as the system finds it: its last path part, in lower case (macOS and Windows file names
    ignore case, so `Bash` and `GIT` run bash and git)."""
    return os.path.basename(word).lower()


def _unwrap(argv, env):
    """Remove wrapper commands and their options. Returns (argv, env)."""
    guard = 0
    while argv and guard < 10:
        guard += 1
        name = _base(argv[0])
        if name not in WRAPPERS:
            break
        takes = WRAPPERS[name]
        i = 1
        if name == "command" and len(argv) > 1 and argv[1] in ("-v", "-V"):
            return [], env  # a lookup, not a run
        if name == "xcrun" and any(a in ("-f", "--find") for a in argv[1:2]):
            return [], env  # a lookup, not a run
        if name == "script":
            argv = _script_command(argv)
            continue
        while i < len(argv):
            a = argv[i]
            if name == "env" and a.startswith("--"):
                a = argv[i] = _env_long(a)
            if name == "env" and _is_assignment(a):
                k, v = a.split("=", 1)
                env[k] = v
                i += 1
                continue
            if a == "--":
                i += 1
                break
            if name == "env" and (a in ("--split-string", "-S") or a.startswith("--split-string=")
                                  or (a[:1] == "-" and a[1:2] != "-" and _env_split_at(a, takes) > 0)):
                # env -S splits its value into words and runs them: the value is the command. It can end a
                # cluster (-iS).
                if a.startswith("--"):
                    whole = a == "--split-string"
                    value = a.split("=", 1)[1] if not whole else ""
                else:
                    value = a[_env_split_at(a, takes) + 1:]
                    whole = not value
                if whole:
                    value = argv[i + 1] if i + 1 < len(argv) else ""
                words = _env_split(value)
                if words is None:
                    return None, env  # berth cannot split it the way env would; the caller asks.
                argv = argv[:i] + words + argv[i + (2 if whole else 1):]
                continue
            if a.startswith("-") and len(a) > 1:
                i += _wrapper_option_width(a, takes)
                continue
            if name in ("timeout", "gtimeout") and a[:1].isdigit():
                i += 1
                continue
            if name == "nice" and a.lstrip("-").isdigit():
                i += 1
                continue
            break
        argv = argv[i:]
    return argv, env


# GNU env's long options; it accepts any unambiguous prefix of one (--arg for --argv0, --split for --split-string).
_ENV_LONG = ("--argv0", "--chdir", "--unset", "--split-string", "--ignore-environment", "--null", "--debug",
             "--default-signal", "--ignore-signal", "--block-signal", "--list-signal-handling", "--help", "--version")


def _env_long(word):
    key, eq, value = word.partition("=")
    hits = [o for o in _ENV_LONG if o.startswith(key)] if len(key) > 2 else []
    return (hits[0] + eq + value) if len(hits) == 1 else word


def _env_split(value):
    """Words of an env -S string as env splits them: blanks separate, single quotes are literal, double quotes
    keep blanks, and backslash escapes follow env's rules (\\_ is a separator outside quotes and a blank inside
    double quotes; \\c ends the string). None when the string cannot be read exactly: env would refuse it (an
    unclosed quote or an unknown escape), or it names a variable (${VAR}) that env fills in when it runs."""
    words, cur, have = [], [], False
    i, n = 0, len(value)
    quote = None
    escapes = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"',
               "#": "#", "$": "$", " ": " "}
    while i < n:
        ch = value[i]
        if quote == "'":
            if ch == "'":
                quote = None
            elif ch == "\\" and i + 1 < n and value[i + 1] in "\\'":
                i += 1
                cur.append(value[i])
            else:
                cur.append(ch)
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                return None
            nxt = value[i + 1]
            i += 2
            if nxt == "c":
                break
            if nxt == "_":
                if quote == '"':
                    cur.append(" ")
                else:
                    if have:
                        words.append("".join(cur))
                    cur, have = [], False
                continue
            if nxt not in escapes:
                return None
            cur.append(escapes[nxt])
            have = True
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            elif ch == "$" and value[i + 1:i + 2] == "{":
                return None
            else:
                cur.append(ch)
            i += 1
            continue
        if ch == "$" and value[i + 1:i + 2] == "{":
            return None  # Inside double quotes or bare, env -S fills in ${VAR}: the words are not known here.
        if ch in "'\"":
            quote = ch
            have = True
        elif ch in " \t\n":
            if have:
                words.append("".join(cur))
            cur, have = [], False
        elif ch == "#" and not have:
            break  # A comment, as env reads it at the start of a word.
        else:
            cur.append(ch)
            have = True
        i += 1
    if quote:
        return None
    if have:
        words.append("".join(cur))
    return words


def _env_split_at(word, takes):
    """Index of -S in an env option cluster (-iS), or 0 when it has none before a letter that takes a value."""
    for k, letter in enumerate(word[1:], 1):
        if letter == "S":
            return k
        if "-" + letter in takes:
            return 0
    return 0


def _wrapper_option_width(word, takes):
    """How many words a wrapper's option spans: 2 when it takes the next word as its value, also as the last
    letter of a cluster (xargs -0I {}, watch -tn 5); else 1."""
    if word in takes:
        return 2
    if word.startswith("--") or "=" in word:
        return 1
    for k, letter in enumerate(word[1:], 1):
        if "-" + letter in takes:
            return 1 if word[k + 1:] else 2
    return 1


# script's short options that take a value (util-linux and BSD together) and long ones that take the next word.
_SCRIPT_VALUE = "EIOBTto"
_SCRIPT_LONG_VALUE = {"--log-in", "--log-out", "--log-io", "--log-timing", "--logging-format", "--echo",
                      "--output-limit"}


def _script_command(argv):
    """The command `script` runs: `script -c CMD [file]` (util-linux) runs CMD with a shell, and
    `script [options] file command ...` (BSD, macOS) runs the words after the file. [] when it only starts an
    interactive shell."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a in ("-c", "--command"):
            return ["sh", "-c", argv[i + 1] if i + 1 < len(argv) else ""]
        if a.startswith("--command="):
            return ["sh", "-c", a.split("=", 1)[1]]
        if a.startswith("--"):
            i += 2 if a in _SCRIPT_LONG_VALUE else 1
            continue
        if a.startswith("-") and len(a) > 1:
            step = 1
            for k, letter in enumerate(a[1:], 1):
                if letter == "c" or letter in _SCRIPT_VALUE:
                    value = a[k + 1:] or (argv[i + 1] if i + 1 < len(argv) else "")
                    if letter == "c":
                        return ["sh", "-c", value]
                    step = 1 if a[k + 1:] else 2
                    break
            i += step
            continue
        break
    return argv[i + 1:]


def _expand_path(word, cwd, home):
    """Expand ~ and $HOME-style references and make a path absolute."""
    w = word
    if w.startswith("~/") or w == "~":
        w = home + w[1:]
    for var in ("$HOME", "${HOME}"):
        if w.startswith(var):
            w = home + w[len(var):]
    for var, val in (("$CLAUDE_PLUGIN_DATA", os.environ.get("CLAUDE_PLUGIN_DATA")),
                     ("${CLAUDE_PLUGIN_DATA}", os.environ.get("CLAUDE_PLUGIN_DATA")),
                     ("$CLAUDE_PLUGIN_ROOT", os.environ.get("CLAUDE_PLUGIN_ROOT")),
                     ("${CLAUDE_PLUGIN_ROOT}", os.environ.get("CLAUDE_PLUGIN_ROOT")),
                     ("$CLAUDE_PROJECT_DIR", os.environ.get("CLAUDE_PROJECT_DIR")),
                     ("${CLAUDE_PROJECT_DIR}", os.environ.get("CLAUDE_PROJECT_DIR"))):
        if val and w.startswith(var):
            w = val + w[len(var):]
    if not os.path.isabs(w):
        w = os.path.join(cwd or os.getcwd(), w)
    return os.path.normpath(w)


def expand_path(word, cwd, home=None):
    return _expand_path(word, cwd, home or os.path.expanduser("~"))


def parse(command, cwd=None, home=None, _depth=0, _result=None):
    """Parse a command line into a ParseResult with a flat list of Cmd.

    A command too large or too deeply nested to read sets result.problem (with problem_level ask or deny) instead.
    """
    home = home or os.path.expanduser("~")
    if _result is not None:
        _parse(command, cwd, home, _depth, _result)
        return _result
    result = ParseResult()
    if len(command or "") > MAX_COMMAND:
        result.flag("ask", "The command is %d KB, more than the %d KB checkrein reads, so it was not checked."
                    % (len(command) // 1024, MAX_COMMAND // 1024))
        return result
    try:
        _parse(command, cwd, home, _depth, result)
    except _Limit as exc:
        result.flag("deny", str(exc))
    except RecursionError:
        result.flag("deny", _DEPTH_REASON)
    return result


def _parse(command, cwd, home, depth, result):
    if not command:
        return
    tokens, inners = _Lexer(command, result, depth).run()
    for inner in inners:
        _commands(inner, cwd, home, depth + 1, result)
    _commands(tokens, cwd, home, depth, result)


def _commands(tokens, cwd, home, depth, result):
    current = []
    groups = []  # (tokens, whether a pipe feeds this command)
    piped = False
    for tok in tokens:
        if isinstance(tok, _Op) and tok in SEPARATORS:
            if current:
                groups.append((current, piped))
                piped = tok in _PIPES
            elif tok not in (";", "(", "{"):
                piped = tok in _PIPES  # A newline or group opener after | still continues the pipeline.
            current = []
        else:
            current.append(tok)
    if current:
        groups.append((current, piped))

    prev_out = None
    for group, piped in groups:
        env = {}
        redirects = []
        stdin = []
        argv = []
        i = 0
        while i < len(group):
            tok = group[i]
            if isinstance(tok, _Op):
                has_target = i + 1 < len(group) and not isinstance(group[i + 1], _Op)
                target = group[i + 1] if has_target else ""
                redirects.append((str(tok), str(target)))
                if isinstance(target, _HereDoc):
                    stdin.append(target.body)
                elif tok == "<<<":
                    stdin.append(target + "\n")
                i += 2 if has_target else 1
                continue
            if not argv and not env and tok in RESERVED:
                i += 1
                continue
            if not argv and _is_assignment(tok):
                k, v = tok.split("=", 1)
                env[k] = v
                i += 1
                continue
            argv.append(tok)
            i += 1
        # A trailing fd number before a redirect (2>file) stays as an argument; drop pure digits.
        argv = [a for a in argv if not (a.isdigit() and len(a) == 1 and redirects)] if redirects else argv
        argv, env = _unwrap(argv, env)
        if argv is None:
            result.flag("ask", _ENV_SPLIT_REASON)
            result.env_split = True
            argv = []
        stdin_text = "".join(stdin)
        piped_text = prev_out if piped else _NO_PIPE
        prev_out = _stage_output(argv, stdin_text, piped_text)
        if not argv:
            if redirects:
                result.cmds.append(Cmd([":"], env, redirects, cwd, depth, stdin_text))
            continue
        name = _base(argv[0])
        if name in ("cd", "pushd") and len(argv) > 1 and not argv[1].startswith("-"):
            cwd = _expand_path(argv[1], cwd, home)
            continue
        if name in SHELLS:
            script = _shell_c_arg(argv)
            if script is not None:
                _parse(script, cwd, home, depth + 1, result)
                continue
            # With no -c, a shell runs the script it reads on stdin.
            _parse(stdin_text, cwd, home, depth + 1, result)
            if not stdin_text and _reads_stdin(argv):
                _piped_script(name, piped_text, cwd, home, depth, result)
        if name in ("source", ".") and len(argv) > 1 and argv[1] in STDIN_PATHS:
            _parse(stdin_text, cwd, home, depth + 1, result)
            if not stdin_text:
                _piped_script(name, piped_text, cwd, home, depth, result)
        if name == "eval":
            _parse(" ".join(argv[1:]), cwd, home, depth + 1, result)
            continue
        result.cmds.append(Cmd(argv, env, redirects, cwd, depth, stdin_text))


def _piped_script(name, piped_text, cwd, home, depth, result):
    """A shell reading its script from a pipe runs what the stage before it printed."""
    if piped_text is _NO_PIPE:
        return
    if piped_text is None:
        result.flag("ask", _UNKNOWN_PIPE % name)
        return
    _parse(piped_text, cwd, home, depth + 1, result)


def _stage_output(argv, stdin_text, piped_text):
    """What a pipeline stage prints, when it is literal text known from the command line; else None."""
    name = _base(argv[0]) if argv else ""
    args = argv[1:]
    if name in ("cat", "tee"):
        if name == "cat" and any(not a.startswith("-") for a in args):
            return None  # cat of a file prints text checkrein cannot see.
        if stdin_text:
            return stdin_text
        return None if piped_text is _NO_PIPE else piped_text
    if name in ("echo", "printf"):
        if any("$" in a or "`" in a or _SUBST in a or _ARITH in a for a in args):
            return None
        # printf formats and escapes (%s, \163, \x20), and echo escapes in shells that expand them, change the
        # text; only a plain \n is decoded here, anything else is unknown.
        if name == "printf" and any("%" in a for a in args):
            return None
        if any("\\" in a.replace("\\n", "") for a in args):
            return None
        while name == "echo" and args and args[0] in ("-n", "-e", "-E", "-ne", "-en"):
            args = args[1:]
        return " ".join(args).replace("\\n", "\n") + "\n"
    return None


def _reads_stdin(argv):
    """True when a shell takes its script from stdin: no script file, or -s, or -."""
    if _base(argv[0]) == "busybox" and len(argv) > 1 and argv[1] in SHELLS:
        argv = argv[1:]
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-":
            return True
        if a == "--":
            return i + 1 >= len(argv)
        if a.startswith("-") and not a.startswith("--") and "s" in a[1:]:
            return True
        if a in _SHELL_VALUE_OPTIONS:
            i += 2
            continue
        if a.startswith(("-", "+")):
            i += 1
            continue
        return False  # A script file: stdin is its data.
    return True


def _shell_c_arg(argv):
    for i, a in enumerate(argv[1:], 1):
        if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
            return argv[i + 1] if i + 1 < len(argv) else ""
    return None


def short_flags(arg, takes_value=""):
    """Letters in a short-option cluster like -fd, stopping at a value-taking letter."""
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 2:
        return ""
    letters = []
    for ch in arg[1:]:
        letters.append(ch)
        if ch in takes_value:
            break
    return "".join(letters)
