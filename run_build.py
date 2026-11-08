"""Git workflow runner for isolated Egern build and publication stages."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import time
from urllib.parse import quote, unquote, urlsplit

PUBLIC = {
    "converter": "https://github.com/errbin/egern-converter.git",
    "sources": "https://github.com/errbin/egern-sources.git",
}

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
        sanitizer=LogSanitizer()
        for stream in (result.stdout,result.stderr):
            if stream:
                print(sanitizer.clean(stream.decode("utf-8","replace")),flush=True)
        if result.returncode:
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

def run_with_heartbeat(args, *, cwd, env, log_path, stage, interval=60, private_markers=()):
    started=time.monotonic()
    sanitizer=LogSanitizer(env,private_markers)
    print(f"{stage} started; output is streamed with private repository details and credentials redacted.",flush=True)
    with Path(log_path).open("wb") as log:
        process=subprocess.Popen(args,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0)
        lines=queue.Queue(maxsize=256)
        def capture_output():
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    lines.put(line)
            finally:
                lines.put(None)
                process.stdout.close()
        reader=Thread(target=capture_output,daemon=True)
        reader.start()
        last_progress=""
        deadline=time.monotonic()+interval
        while True:
            try:
                line=lines.get(timeout=max(0.05,deadline-time.monotonic()))
                if line is None:
                    break
                decoded=line.decode("utf-8","replace").rstrip("\r\n")
                message=public_progress(decoded) or decoded
                message=sanitizer.clean(message)
                if message:
                    last_progress=message.splitlines()[-1]
                    print(message,flush=True)
            except queue.Empty:
                elapsed=int(time.monotonic()-started)
                suffix=f" Last progress: {last_progress}" if last_progress else ""
                print(f"{stage} is still running; elapsed {elapsed // 60}m {elapsed % 60}s.{suffix}",flush=True)
                deadline=time.monotonic()+interval
        reader.join()
        return process.wait()


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
    lock=json.loads((core/"toolchain.lock.json").read_text(encoding="utf-8"))
    repos={}
    for role,url in PUBLIC.items():
        revision=lock[role]
        if not re.fullmatch(r"[0-9a-f]{40}",revision):raise ValueError("Public toolchain revisions must be pinned commit IDs")
        dest=root/role
        execute(["git","init","--quiet",str(dest)])
        execute(["git","-C",str(dest),"fetch","--quiet","--depth=1",url,revision])
        execute(["git","-C",str(dest),"checkout","--quiet","--detach","FETCH_HEAD"])
        repos[role]=dest
    community=Path(__file__).resolve().parent
    verify_api_free(repos["converter"]/"tools",repos["sources"]/"tools",community/".github",Path(__file__).resolve(),allow_api_files=(community/".github/scripts/prune_workflow_runs.py",))
    execute([sys.executable,"tools/validate.py"],cwd=repos["sources"])
    execute([sys.executable,"-m","unittest","discover","-s","tests","-p","test_catalog*.py"],cwd=repos["sources"])
    clone(os.environ.get("UPSTREAM_REPOSITORY"),upstream,os.environ.get("UPSTREAM_READ_KEY"))
    runtime=root/"runtime"
    execute([sys.executable,str(core/"assemble.py"),"--converter",str(repos["converter"]),"--sources",str(repos["sources"]),"--archive",str(upstream),"--output",str(runtime)])
    output("job_root",str(root))
    print("Pinned source repositories and prepared isolated build input.")

def build():
    root=job_root();runtime=root/"runtime";dist=root/"site-output";raw=root/"captured-upstream"
    if not runtime.is_dir():raise ValueError("Build input is missing")
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1",PYTHONUNBUFFERED="1",EGERN_PROGRESS_EVENTS="1",EGERN_RAW_ROOT=str(raw))
    private=private_markers(os.environ.get("BUILD_REPOSITORY"))+private_markers(os.environ.get("UPSTREAM_REPOSITORY"))
    result=run_with_heartbeat([sys.executable,"build.py","--verify","--output",str(dist),"--base-url","https://egern.pages.dev"],cwd=runtime,env=env,log_path=root/"build.log",stage="Build and differential verification",private_markers=private)
    if result:
        raise RuntimeError("Source synchronization, tests, conversion or differential validation failed; previous website remains intact")
    tests=run_with_heartbeat([sys.executable,"-m","unittest","discover","-s","tests","-v"],cwd=runtime,env=env,log_path=root/"tests.log",stage="Full regression tests",private_markers=private)
    if tests:raise RuntimeError("Full converter and build regression suite failed; see diagnostics above")
    print("Source synchronization, conversion, differential verification and full regression suite passed.")

def persist_upstream():
    root=job_root();archive=root/"upstream";raw=root/"captured-upstream";runtime=root/"runtime"
    if not archive.is_dir():raise ValueError("Upstream archive checkout is missing")
    for name in ["raw","manifests"]:
        src=raw/name
        if src.exists():shutil.copytree(src,archive/name,dirs_exist_ok=True)
    sys.path.insert(0,str(runtime))
    from tools.egern_hub.raw_archive import archive_snapshot
    archive_snapshot(runtime/"sources",archive)
    for name in ["cache","data"]:
        src=runtime/name
        if src.exists():shutil.copytree(src,archive/"state"/name,dirs_exist_ok=True)
    for name in ["icons","avatars","previews"]:
        src=runtime/"web/assets"/name
        if src.exists():shutil.copytree(src,archive/"state/artwork"/name,dirs_exist_ok=True)
    push_repository(archive,os.environ.get("UPSTREAM_REPOSITORY"),os.environ.get("UPSTREAM_WRITE_KEY"),"main")
    print("Captured original source bytes and build state by Git.")

def push_repository(directory, destination, key, branch):
    if not destination or not key:raise ValueError("Missing publication configuration")
    root=Path(directory)
    if not (root/".git").is_dir():raise ValueError("Publication checkout is missing")
    execute(["git","-C",str(root),"config","user.name","errbin"])
    execute(["git","-C",str(root),"config","user.email","errbin@users.noreply.github.com"])
    execute(["git","-C",str(root),"add","--all"])
    status=execute(["git","-C",str(root),"status","--porcelain"])
    if status:
        execute(["git","-C",str(root),"commit","--quiet","-m", "Update upstream snapshot" if branch=="main" else "Publish verified website"])
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
    result=execute(["git","-C",str(target),"ls-remote","origin","refs/heads/production"],key=key)
    if result:
        execute(["git","-C",str(target),"fetch","--quiet","origin","production"],key=key)
        execute(["git","-C",str(target),"checkout","--quiet","-B","production","FETCH_HEAD"])
    else:execute(["git","-C",str(target),"checkout","--quiet","-b","production","main"])
    for p in list(target.iterdir()):
        if p.name==".git":continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(sanitized,target,dirs_exist_ok=True)
    push_repository(target,address,key,"production")
    print("Verified website published by Git.")

def main():
    parser=argparse.ArgumentParser();parser.add_argument("stage",choices=["prepare","build","persist-upstream","publish"])
    stage=parser.parse_args().stage
    {"prepare":prepare,"build":build,"persist-upstream":persist_upstream,"publish":publish}[stage]()
if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(LogSanitizer().clean(type(exc).__name__+": "+str(exc)),file=sys.stderr);sys.exit(1)
