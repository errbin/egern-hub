"""Git workflow runner for isolated Egern build and publication stages."""
from __future__ import annotations
import argparse
from collections import deque
import filecmp
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit

PUBLIC = {
    "converter": "https://github.com/errbin/egern-converter.git",
    "sources": "https://github.com/errbin/egern-sources.git",
}
# GitHub 单文件上限为 100 MB，发布前保留一半安全余量。
PUBLISHED_FILE_MAX_BYTES=50*1024*1024

def execute(args, *, key=None, cwd=None, data=None):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    key_dir=None
    if key is not None:
        key_dir=Path(tempfile.mkdtemp(prefix="egern-ssh-"))
        key_path=key_dir/"key"
        key_path.write_text(key.rstrip() + "\n", encoding="utf-8")
        key_path.chmod(0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR"
    try:
        result = subprocess.run(args, cwd=cwd, env=env, input=data, capture_output=True)
        diagnostics=ErrorLogFilter(LogSanitizer())
        for stream in (result.stdout,result.stderr):
            if stream:
                for line in stream.decode("utf-8","replace").splitlines():
                    diagnostics.write(line)
        if result.returncode:
            diagnostics.finish(result.returncode)
            raise RuntimeError(f"Command failed with exit code {result.returncode}; see diagnostics above")
        return result.stdout.decode("utf-8", "replace").strip()
    finally:
        if key_dir is not None:shutil.rmtree(key_dir,ignore_errors=True)

def clone(url, path, key):
    if not url or not key:
        raise ValueError("Missing repository connection configuration")
    execute(["git", "clone", "--quiet", "--depth=1", "--branch=main", url, str(path)], key=key)

def job_root():
    root=Path(os.environ.get("RUNNER_TEMP",tempfile.gettempdir()))/"egernlab-job"
    return root

def output(name, value):
    path=os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path,"a",encoding="utf-8") as f:f.write(f"{name}={value}\n")

def run_with_error_logs(args, *, cwd, env, log_path, private_markers=()):
    diagnostics=ErrorLogFilter(LogSanitizer(env,private_markers))
    with Path(log_path).open("wb") as log:
        Path(log_path).chmod(0o600)
        process=subprocess.Popen(args,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0)
        with process.stdout:
            for line in process.stdout:
                log.write(line)
                log.flush()
                diagnostics.write(line.decode("utf-8","replace").rstrip("\r\n"))
        result=process.wait()
        diagnostics.finish(result)
        return result


class ErrorLogFilter:
    """Stream errors and traceback context; retain a bounded failure fallback."""
    marker=re.compile(r"\b(?:[\w.]*(?:Error|Exception)\b|Traceback\b|FAIL(?:ED)?\b|ERROR\b|FATAL\b)|\b(?:error|fatal|critical)\s*:|\bHTTP(?:\s+Error)?\s+[45]\d\d\b|\b(?:failed|failure|permission denied|connection refused|timed out)\b",re.I)
    failures={"failed","mismatch","expectation_failed","original_error","converted_error"}

    def __init__(self,sanitizer):
        self.sanitizer=sanitizer
        self.traceback=False
        self.test_failure=None
        self.test_summary=None
        self.emitted=False
        self.tail=deque(maxlen=20)

    def write(self,line):
        # Sanitize every line, including hidden key bodies, to preserve redaction state.
        line=self.sanitizer.clean(line)
        if re.match(r"^(?:FAIL|ERROR):\s+\S",line):
            self.test_failure="header"
            self.emit(line)
            return
        if self.test_failure:
            if re.fullmatch(r"={5,}",line):
                self.test_failure=None
            elif re.fullmatch(r"-{5,}",line):
                self.test_failure="body" if self.test_failure=="header" else None
            else:
                self.emit(line)
            return
        if re.fullmatch(r"Ran \d+ tests? in .+",line):
            self.test_summary=line
            return
        if line.startswith("FAILED (") and self.test_summary:
            self.emit(self.test_summary)
            self.test_summary=None
        if line.startswith("@@EGERN_PROGRESS "):
            self.traceback=False
            try:event=json.loads(line[len("@@EGERN_PROGRESS "):])
            except (TypeError,ValueError):return
            if not isinstance(event,dict):return
            if not isinstance(event.get("stage"),str):return
            result=event.get("result")
            status=result.get("status") if isinstance(result,dict) else event.get("status")
            if event.get("stage") not in {"conversion_failed","probe_generation_skipped"} and (not isinstance(status,str) or status not in self.failures):return
            message=public_progress(line)
            if message:self.emit(self.sanitizer.clean(message))
            return
        if line:self.tail.append(line)
        if line.startswith("Building: "):return
        if re.match(r"^test\S+.* \.\.\. (?:ok|skipped\b)",line):return
        continuation=self.traceback and (not line.strip() or line[:1].isspace())
        self.traceback=line.startswith("Traceback ") or continuation
        if self.marker.search(line) or continuation:
            self.emit(line)

    def emit(self,line):
        print(line,flush=True)
        self.emitted=True

    def finish(self,returncode):
        if returncode and not self.emitted:
            self.emit(f"Command failed with exit code {returncode}; last diagnostic lines:")
            for line in self.tail:self.emit(line)


class LogSanitizer:
    """Redact configured private repositories and credentials, preserving diagnostics."""
    def __init__(self, env=None, markers=()):
        env=os.environ if env is None else env
        hidden=set(markers)
        for name in ("BUILD_REPOSITORY","UPSTREAM_REPOSITORY","SITE_REPOSITORY"):
            address=env.get(name,"")
            hidden.update(private_markers(address))
            if address:
                path=re.split(r"[:/]",address.rstrip("/"))[-1]
                hidden.add(path)
                hidden.add(path.removesuffix(".git"))
        self.repositories=sorted((value for value in hidden if value),key=len,reverse=True)
        credentials=set()
        for name,value in env.items():
            if value and re.search(r"(?:TOKEN|PASSWORD|SECRET|(?:^|_)KEY)(?:_|$)",name,re.I):
                credentials.add(value)
                credentials.update(part.strip() for part in value.splitlines() if part.strip())
        self.credentials=sorted(credentials,key=len,reverse=True)
        self.in_private_key=False

    def redact(self, text):
        for value in self.credentials:
            text=text.replace(value,"[REDACTED CREDENTIAL]")
        for value in self.repositories:
            text=re.sub(re.escape(value),"[PRIVATE REPOSITORY]",text,flags=re.I)
        text=re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)","[REDACTED CREDENTIAL]",text)
        return re.sub(r"(https?://)[^/\s@]+@",r"\1[REDACTED CREDENTIAL]@",text)

    def clean(self, text):
        def encoded(match):
            original=match.group()
            decoded=unquote(unquote(original))
            masked=self.redact(decoded)
            # Preserve ordinary URLs exactly; decode only to identify secrets.
            return original if masked==decoded else masked
        text=re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]","",text)
        lines=[]
        for line in text.splitlines():
            if re.search(r"-----BEGIN .*PRIVATE KEY-----",line):self.in_private_key=True
            if self.in_private_key:
                if re.search(r"-----END .*PRIVATE KEY-----",line):self.in_private_key=False
                lines.append("[REDACTED PRIVATE KEY]")
                continue
            line=self.redact(re.sub(r"\S*%[0-9a-fA-F]{2}\S*",encoded,line))
            # Upstream text must not be interpreted as Actions workflow commands.
            lines.append(line.replace("::",":\u200b:"))
        return "\n".join(lines)


def public_progress(line):
    phases={
        "Discover upstream modules and dependencies":"Discovering upstream modules and dependencies",
        "Synchronize source files and dates":"Synchronizing source files and dates",
        "Convert all scripts and verify":"Preparing script conversion and verification",
        "Extract module and author metadata":"Extracting module and author metadata",
        "Resolve artwork and previews":"Resolving artwork and previews",
        "Generate static site":"Generating the static site",
    }
    phase=line.rstrip("\r\n").removeprefix("Building: ")
    if phase in phases and line.startswith("Building: "):
        return f"Build phase: {phases[phase]}"
    prefix="@@EGERN_PROGRESS "
    if not line.startswith(prefix):return None
    try:event=json.loads(line[len(prefix):])
    except (TypeError,ValueError):return None
    if not isinstance(event,dict):return None
    kind,stage=event.get("kind"),event.get("stage")
    label=event.get("id")
    if not isinstance(label,str) or not label:return None
    if isinstance(event.get("url"),str) and event["url"]:
        label+=f" [{event['url']}]"
    def count(name):
        value=event.get(name)
        return value if isinstance(value,int) and not isinstance(value,bool) and 0<=value<=1_000_000 else None
    index,total=count("index"),count("total")
    position=f" ({index}/{total})" if index is not None and total is not None else ""
    if kind=="source":
        if stage in {"reading_repository_file","read_repository_file"}:
            method={"cache":"local cache","archive":"repository snapshot","download":"network download"}.get(event.get("method"))
            verb="Reading" if stage=="reading_repository_file" else "Read"
            suffix=f" ({method})" if method else ""
            return f"{verb} source file{suffix}: {label}"
        messages={
            "checking":f"Checking source: {label}",
            "fetching":f"Fetching source: {label}",
            "fetched":f"Fetched source: {label}",
            "current":f"Source is current: {label}",
            "checking_history":f"Checking source history: {label}",
            "history_checking":f"Checking source history file: {label}",
            "history_checked":f"Checked source history file: {label}",
        }
        if stage in {"telegram_fetching_page","telegram_page_fetched"}:
            page=count("page")
            if page is None:return None
            verb="Fetching" if stage=="telegram_fetching_page" else "Fetched"
            messages=count("messages")
            suffix=f", {messages} messages seen" if messages is not None else ""
            return f"{verb} Telegram source @{label}, page {page}{suffix}"
        if stage=="telegram_scan_completed":
            pages,messages=count("pages"),count("messages")
            if pages is None or messages is None:return None
            return f"Finished Telegram source @{label}: {pages} pages, {messages} messages"
        return messages.get(stage)
    if kind=="script":
        if stage=="conversion_failed":
            return f"Conversion failed: {label}{position}: {event.get('reason','')}"
        messages={
            "converting":f"Converting script: {label}{position}",
            "converted":f"Converted script: {label}{position}",
            "verification_probes":f"Preparing verification: {label}{position}",
            "probes_ready":f"Verification probes ready: {label}{position}",
            "probe_generation_skipped":f"Verification probes skipped: {label}{position}: {event.get('reason','')}",
        }
        return messages.get(stage)
    if kind=="verification":
        if stage=="started":return f"Verifying script: {label}"
        if stage=="case_started":return f"Verifying case: {label}: {event.get('name','')}"
        if stage=="case_completed":return f"Verification case: {label}: "+json.dumps(event.get('result',{}),ensure_ascii=False)
        if stage=="completed":
            status=event.get("status")
            if status not in {"passed","failed","not_covered","partial","probe_passed","baseline_only","inconclusive"}:return None
            return f"Verified script: {label} ({status})"
    return None

def verify_api_free(*roots, allow_api_files=()):
    forbidden=("api"+"."+"github"+"."+"com","github"+".com/api/v3","github"+".com/api/graphql","github"+"_json","github"+"_token","gh_"+"token","gh"+" api","octo"+"kit","py"+"github","gh"+"api")
    suffixes={".py",".mjs",".cjs",".js",".html",".json",".yml",".yaml",".sh"}
    allowed={Path(path).resolve() for path in allow_api_files}
    files=[]
    for root in roots:
        root=Path(root)
        if root.is_file():files.append(root)
        elif root.exists():files.extend(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes and ".git" not in p.parts and "node_modules" not in p.parts)
    for path in files:
        if path.resolve() in allowed:continue
        lowered=path.read_text(encoding="utf-8",errors="replace").lower()
        if any(term in lowered for term in forbidden):raise ValueError("GitHub API dependency detected in project code")
        if path.suffix in {".yml",".yaml"} and re.search(r"(?m)^\s*uses:\s*",lowered):
            raise ValueError("External workflow actions are disabled; audit their repository transport first")

def private_markers(address):
    if not address:return []
    match=re.fullmatch(r"(?:ssh://)?git@([^:/]+)[:/]([^\s]+)",address)
    if match:
        host,path=match.groups()
    else:
        parsed=urlsplit(address)
        host=parsed.hostname or ""
        path=parsed.path.lstrip("/") if parsed.scheme else address
    path=path.rstrip("/")
    markers={address, path}
    if path.endswith(".git"):markers.add(path[:-4])
    if host:
        markers.add(host+"/"+path)
        if path.endswith(".git"):markers.add(host+"/"+path[:-4])
    markers.update(quote(marker,safe="") for marker in tuple(markers))
    return sorted(marker for marker in markers if marker)

def prepare():
    root=job_root()
    if root.exists():shutil.rmtree(root)
    root.mkdir(parents=True)
    core=root/"build-engine";sources=root/"sources";upstream=root/"upstream"
    clone(os.environ.get("BUILD_REPOSITORY"),core,os.environ.get("BUILD_READ_KEY"))
    verify_api_free(core/"build.py",core/"tools",core/"web")
    repos={};resolved={}
    for role,url in PUBLIC.items():
        dest=root/role
        execute(["git","init","--quiet",str(dest)])
        execute(["git","-C",str(dest),"fetch","--quiet","--depth=1",url,"HEAD"])
        execute(["git","-C",str(dest),"checkout","--quiet","--detach","FETCH_HEAD"])
        revision=execute(["git","-C",str(dest),"rev-parse","HEAD"])
        if not re.fullmatch(r"[0-9a-f]{40}",revision):raise ValueError("Public toolchain checkout did not resolve to a commit ID")
        repos[role]=dest;resolved[role]=revision
    (root/"toolchain.json").write_text(json.dumps(resolved,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    output("toolchain",json.dumps(resolved,sort_keys=True))
    community=Path(__file__).resolve().parent
    verify_api_free(repos["converter"]/"tools",repos["sources"]/"tools",community/".github",Path(__file__).resolve(),allow_api_files=(community/".github/scripts/prune_workflow_runs.py",))
    execute([sys.executable,"tools/validate.py"],cwd=repos["sources"])
    execute([sys.executable,"-m","unittest","discover","-s","tests","-p","test_catalog*.py"],cwd=repos["sources"])
    clone(os.environ.get("UPSTREAM_REPOSITORY"),upstream,os.environ.get("UPSTREAM_READ_KEY"))
    runtime=root/"runtime"
    execute([sys.executable,str(core/"assemble.py"),"--converter",str(repos["converter"]),"--sources",str(repos["sources"]),"--archive",str(upstream),"--output",str(runtime)])
    output("job_root",str(root))

def build():
    root=job_root();runtime=root/"runtime";dist=root/"site-output";raw=root/"captured-upstream"
    if not runtime.is_dir():raise ValueError("Build input is missing")
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1",PYTHONUNBUFFERED="1",EGERN_PROGRESS_EVENTS="1",EGERN_RAW_ROOT=str(raw))
    private=private_markers(os.environ.get("BUILD_REPOSITORY"))+private_markers(os.environ.get("UPSTREAM_REPOSITORY"))
    result=run_with_error_logs([sys.executable,"build.py","--verify","--isolate-script-failures","--output",str(dist),"--base-url","https://egern.pages.dev"],cwd=runtime,env=env,log_path=root/"build.log",private_markers=private)
    if result:
        raise RuntimeError("Source synchronization, tests, conversion or differential validation failed; previous website remains intact")

def check_tests(domain):
    root=job_root();runtime=root/"runtime"
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1",PYTHONUNBUFFERED="1")
    private=private_markers(os.environ.get("BUILD_REPOSITORY"))+private_markers(os.environ.get("UPSTREAM_REPOSITORY"))
    result=run_with_error_logs([sys.executable,"-m","tools.egern_hub.ci",domain],
        cwd=runtime,env=env,log_path=root/("tests-"+domain+".log"),private_markers=private)
    if result:raise RuntimeError(domain.capitalize()+" regression checks failed; see diagnostics above")

SYNC_STATS=("copied_files","copied_bytes","metadata_files","deleted_files","deleted_dirs")

def _sync_stats(stats):
    stats={} if stats is None else stats
    for key in SYNC_STATS:stats.setdefault(key,0)
    return stats

def _remove_path(path):
    mode=path.lstat().st_mode
    if stat.S_ISDIR(mode):shutil.rmtree(path)
    else:path.unlink()

def _ensure_directory(root,relative,ensured):
    if relative in ensured:return
    current=root
    for part in relative.parts:
        current=current/part
        try:mode=current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir()
            continue
        if stat.S_ISDIR(mode):continue
        # 目标侧若被链接或文件占用，先移除该节点，避免后续写入穿透到目标树之外。
        _remove_path(current)
        current.mkdir()
    ensured.add(relative)

def _source_files(source):
    files={};pending=[(source,Path())]
    while pending:
        directory,relative=pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                item=relative/entry.name;source_stat=entry.stat(follow_symlinks=False);mode=source_stat.st_mode
                if item.parts and item.parts[0]=='.git':
                    raise ValueError("Upstream snapshots cannot contain Git metadata")
                if stat.S_ISLNK(mode):
                    raise ValueError(f"Upstream snapshots cannot contain symlinks: {item.as_posix()}")
                if stat.S_ISDIR(mode):pending.append((Path(entry.path),item))
                elif stat.S_ISREG(mode):files[item]=source_stat
                else:raise ValueError(f"Upstream snapshots contain a non-regular file: {item.as_posix()}")
    return files

def _target_files(target):
    files={};directories=[];pending=[(target,Path())]
    while pending:
        directory,relative=pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                item=relative/entry.name;mode=entry.stat(follow_symlinks=False).st_mode
                if item.parts and item.parts[0]=='.git':continue
                if stat.S_ISDIR(mode):
                    directories.append(item)
                    pending.append((Path(entry.path),item))
                else:files[item]=Path(entry.path)
    return files,directories

def sync_tree(source,target,*,stats=None,delete_missing=True,sync_metadata=True):
    """Copy changed files from a source tree and remove exact target extras."""
    source,target=Path(source),Path(target);stats=_sync_stats(stats)
    source_mode=source.lstat().st_mode
    if stat.S_ISLNK(source_mode) or not stat.S_ISDIR(source_mode):
        raise ValueError("Upstream snapshot source must be a real directory")
    try:target_mode=target.lstat().st_mode
    except FileNotFoundError:
        target.mkdir(parents=True)
    else:
        if stat.S_ISLNK(target_mode) or not stat.S_ISDIR(target_mode):
            raise ValueError("Upstream snapshot target must be a real directory")
    expected=_source_files(source)
    existing,directories=_target_files(target)
    ensured=set()
    if delete_missing:
        for relative,path in existing.items():
            if relative not in expected:
                path.unlink()
                stats["deleted_files"]+=1
    for relative,source_stat in expected.items():
        destination=target/relative;source_path=source/relative
        _ensure_directory(target,relative.parent,ensured)
        try:target_stat=destination.lstat()
        except FileNotFoundError:target_stat=None
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            _remove_path(destination)
            target_stat=None
        copy=False
        if target_stat is None:
            copy=True
        elif target_stat.st_size!=source_stat.st_size:
            copy=True
        elif target_stat.st_mtime_ns!=source_stat.st_mtime_ns or stat.S_IMODE(target_stat.st_mode)!=stat.S_IMODE(source_stat.st_mode):
            if not filecmp.cmp(source_path,destination,shallow=False):
                copy=True
            elif sync_metadata:
                # 内容一致时只修复权限/时间，避免无意义重写和后续重复比较。
                shutil.copystat(source_path,destination,follow_symlinks=False)
                stats["metadata_files"]+=1
        if copy:
            shutil.copy2(source_path,destination,follow_symlinks=False)
            stats["copied_files"]+=1
            stats["copied_bytes"]+=source_stat.st_size
    if delete_missing:
        for relative in sorted(directories,key=lambda item:len(item.parts),reverse=True):
            try:(target/relative).rmdir()
            except OSError:continue
            stats["deleted_dirs"]+=1
    return stats

def persist_upstream():
    root=job_root();archive=root/"upstream";raw=root/"captured-upstream";runtime=root/"runtime"
    if not archive.is_dir():raise ValueError("Upstream archive checkout is missing")
    stats={}
    # 本轮新捕获的 raw/manifests 是增补来源；删除由随后的完整 sources 快照统一负责。
    for name in ["raw","manifests"]:
        src=raw/name
        if src.exists():sync_tree(src,archive/name,stats=stats,delete_missing=False,sync_metadata=False)
    sys.path.insert(0,str(runtime))
    from tools.egern_hub.raw_archive import archive_snapshot
    archive_snapshot(runtime/"sources",archive,stats=stats)
    for name in ["cache","data"]:
        src=runtime/name
        if src.exists():sync_tree(src,archive/"state"/name,stats=stats)
    for name in ["icons","avatars","previews"]:
        src=runtime/"web/assets"/name
        if src.exists():sync_tree(src,archive/"state/artwork"/name,stats=stats)
    push_repository(archive,os.environ.get("UPSTREAM_REPOSITORY"),os.environ.get("UPSTREAM_WRITE_KEY"),"main",message="Update upstream snapshot")

def check_published_file_sizes(directory,max_bytes=None):
    root=Path(directory)
    limit=PUBLISHED_FILE_MAX_BYTES if max_bytes is None else max_bytes
    oversized=[]
    for relative in execute(["git","-C",str(root),"ls-files","-z"]).split("\0"):
        if not relative:continue
        size=(root/relative).lstat().st_size
        if size>limit:oversized.append((relative,size))
    if oversized:
        details=", ".join(f"{relative} ({size:,} bytes)" for relative,size in oversized)
        raise ValueError(f"Publication contains files over {limit:,} bytes: {details}")

def push_repository(directory, destination, key, branch, *, message):
    if not destination or not key:raise ValueError("Missing publication configuration")
    root=Path(directory)
    if not (root/".git").is_dir():raise ValueError("Publication checkout is missing")
    execute(["git","-C",str(root),"config","user.name","errbin"])
    execute(["git","-C",str(root),"config","user.email","errbin@users.noreply.github.com"])
    execute(["git","-C",str(root),"add","--all"])
    # 远端会在 push 时才拒绝超限文件，因此必须在此处提前失败并列出路径与大小。
    check_published_file_sizes(root)
    status=execute(["git","-C",str(root),"status","--porcelain"])
    if status:
        execute(["git","-C",str(root),"commit","--quiet","-m",message])
    execute(["git","-C",str(root),"push","--quiet","origin",f"HEAD:refs/heads/{branch}"],key=key)

def publish():
    root=job_root();dist=root/"site-output";target=root/"site-publication"
    if target.exists():shutil.rmtree(target)
    address=os.environ.get("SITE_REPOSITORY");key=os.environ.get("SITE_WRITE_KEY")
    if not address or not key:raise ValueError("Missing website publication configuration")
    sys.path.insert(0,str(root/"runtime"))
    from publish import prepare as clean_site
    forbidden=[]
    for value in [os.environ.get("BUILD_REPOSITORY"),os.environ.get("UPSTREAM_REPOSITORY"),address]:
        forbidden.extend(private_markers(value))
    sanitized=root/"sanitized-site"
    clean_site(dist,sanitized,forbidden)
    clone(address,target,key)
    for p in list(target.iterdir()):
        if p.name==".git":continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(sanitized,target,dirs_exist_ok=True)
    push_repository(target,address,key,"main",message="Publish verified website")

def main():
    parser=argparse.ArgumentParser();parser.add_argument("stage",choices=["prepare","build","check-site","check-converter","persist-upstream","publish"])
    stage=parser.parse_args().stage
    {"prepare":prepare,"build":build,"check-site":lambda:check_tests("site"),
     "check-converter":lambda:check_tests("conversion"),"persist-upstream":persist_upstream,"publish":publish}[stage]()
if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(LogSanitizer().clean(type(exc).__name__+": "+str(exc)),file=sys.stderr);sys.exit(1)
