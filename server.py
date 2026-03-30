r"""
╔══════════════════════════════════════════════════════════╗
║  ./server.py  —  AI Canvas V2 独立服务器               ║
╠══════════════════════════════════════════════════════════╣
║  启动方式：                                              ║
║    cd v2                                                 ║
║    venv\Scripts\python server.py                        ║
║  浏览器访问：http://localhost:8777                        ║
╠══════════════════════════════════════════════════════════╣
║  目录结构（均在 v2/ 内）：                               ║
║    user/Canvas Project/  — 画布项目文件                  ║
║    user/shortcuts.json   — 快捷键设置                    ║
║    user/settings.json    — 主题等设置                    ║
║    user/config.json      — API Key 配置                  ║
║    data/uploads/         — 上传文件缓存                  ║
╚══════════════════════════════════════════════════════════╝
"""

import http.server
import socketserver
import os
import json
import threading
import subprocess
import time
import mimetypes
import sys
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import unquote
import base64
import re
import random
import hashlib
import datetime

mimetypes.add_type("text/javascript; charset=utf-8", ".js")
mimetypes.add_type("text/javascript; charset=utf-8", ".mjs")
mimetypes.add_type("text/css; charset=utf-8", ".css")

PORT      = int(os.environ.get("AICANVAS_PORT", "8777"))
DIRECTORY = os.path.abspath(os.path.dirname(__file__))   # v2/ 绝对路径
# ── 自动更新 ─────────────────────────────────────────────
# 从 index.html 读取版本号
import re

def get_version_from_index_html():
    """从 index.html 中读取版本号"""
    index_path = os.path.join(DIRECTORY, "index.html")
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 匹配 <meta name="app-version" content="V0.0.7">
        match = re.search(r'<meta name="app-version" content="([^"]+)"', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "V0.0.7"  # 默认版本号

LOCAL_VERSION   = get_version_from_index_html()  # 从 index.html 读取版本号
UPDATE_INTERVAL = 30 * 60          # 检查间隔（秒），默认 30 分钟
_update_info    = None              # None=无更新；dict=有更新信息
_update_lock    = threading.Lock()
_gen_seq_lock   = threading.Lock()

# ── 数据目录（全部在 v2/ 内）────────────────────────────
USER_DIR       = os.path.join(DIRECTORY, "user")
CANVAS_DIR     = os.path.join(USER_DIR,  "Canvas Project")
ASSETS_DIR     = os.path.join(DIRECTORY, "data", "assets")
ASSET_THUMBS_DIR = os.path.join(ASSETS_DIR, "thumbs")
UPLOADS_DIR    = os.path.join(DIRECTORY, "data", "uploads")
OUTPUT_DIR     = os.path.join(DIRECTORY, "output")
CONFIG_FILE    = os.path.join(USER_DIR, "config.json")
GEN_SEQ_STATE_FILE = os.path.join(OUTPUT_DIR, ".gen_seq_state.json")

# 确保目录存在
os.makedirs(CANVAS_DIR,  exist_ok=True)
os.makedirs(ASSETS_DIR,  exist_ok=True)
os.makedirs(ASSET_THUMBS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR,  exist_ok=True)
os.makedirs(USER_DIR,    exist_ok=True)
# ── 自定义 AI 全局配置（环境变量 > config.json）────────────────────────────
# 支持的环境变量：CUSTOM_AI_URL, CUSTOM_AI_KEY
def _get_custom_ai_config():
    """优先读取环境变量，其次 config.json 中 custom_ai 块，再其次根节点配置"""
    env_url = os.environ.get("CUSTOM_AI_URL", "").strip()
    env_key = os.environ.get("CUSTOM_AI_KEY", "").strip()
    
    cfg_url = ""
    cfg_key = ""
    try:
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        ca = cfg.get("custom_ai", {})
        cfg_url = ca.get("apiUrl") or cfg.get("apiUrl", "")
        cfg_key = ca.get("apiKey") or cfg.get("apiKey", "")
    except Exception:
        pass

    final_url = env_url if env_url else cfg_url
    final_key = env_key if env_key else cfg_key
    
    source = "env" if (env_url or env_key) else "config"
    return {"apiUrl": final_url, "apiKey": final_key, "source": source}


# ── 自动更新：后台线程 ──────────────────────────────────
def _parse_remote_info():
    """从 git remote origin URL 解析出 platform/owner/repo/branch
    支持 GitHub / Gitee / 通用 HTTPS 和 SSH 格式
    """
    try:
        raw = subprocess.check_output(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=DIRECTORY, stderr=subprocess.DEVNULL
        ).decode().strip()
        # 解析 owner/repo
        if raw.startswith('https://'):
            parts = raw.rstrip('/').split('/')
            if parts[-1].endswith('.git'):
                parts[-1] = parts[-1][:-4]
            owner, repo = parts[-2], parts[-1]
            host = parts[2]  # e.g. github.com or gitee.com
        else:
            # SSH: git@github.com:owner/repo.git  或 git@gitee.com:owner/repo.git
            host = raw.split('@')[-1].split(':')[0]
            path_part = raw.split(':')[-1]
            if path_part.endswith('.git'):
                path_part = path_part[:-4]
            owner, repo = path_part.split('/')
        branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=DIRECTORY, stderr=subprocess.DEVNULL
        ).decode().strip() or 'master'
        # 识别平台
        if 'gitee.com' in host:
            platform = 'gitee'
        elif 'github.com' in host:
            platform = 'github'
        else:
            platform = 'unknown'
        return platform, owner, repo, branch, host
    except Exception:
        return None, None, None, 'master', None
def _do_update_check():
    """对比本地与远端最新 commit hash；发现更新写入 _update_info，由用户手动触发更新。支持 GitHub 和 Gitee"""
    global _update_info
    
    # 如果存在 .dev 标记文件，说明是本地开发环境，跳过更新检查
    if os.path.exists(os.path.join(DIRECTORY, ".dev")):
        return

    try:
        local_hash = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=DIRECTORY, stderr=subprocess.DEVNULL
        ).decode().strip()
        
        # 硬编码仓库 B 的信息，不再动态读取本地的 git remote
        platform = 'github'
        owner = 'ashuoAI'
        repo = 'AI-CanvasPro'
        branch = 'master'  # 假设发布仓库的主分支是 master，如果是 main 请自行修改
        
        if platform == 'gitee':
            api_url = f"https://gitee.com/api/v5/repos/{owner}/{repo}/commits?sha={branch}&limit=1"
            headers = {'User-Agent': 'TapNow-AutoUpdate/1.0'}
            download_url = f"https://gitee.com/{owner}/{repo}"
            def get_sha(data): return data[0].get('sha', '') if isinstance(data, list) and data else ''
            def get_msg(data): return (data[0].get('commit', {}).get('message', '') if isinstance(data, list) and data else '').split('\n')[0][:80]
        elif platform == 'github':
            api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
            headers = {'User-Agent': 'TapNow-AutoUpdate/1.0', 'Accept': 'application/vnd.github.v3+json'}
            download_url = f"https://github.com/{owner}/{repo}/releases/latest"
            def get_sha(data): return data.get('sha', '')
            def get_msg(data): return data.get('commit', {}).get('message', '').split('\n')[0][:80]
        else:
            # print(f"[AutoUpdate] 不支持的平台: {host}")
            return
        # print(f"[AutoUpdate] 检查中 ({platform}) {owner}/{repo}@{branch}")
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        remote_sha = get_sha(data)
        if not remote_sha or remote_sha == local_hash:
            with _update_lock:
                _update_info = None
            return
        commit_msg = get_msg(data)
        with _update_lock:
            _update_info = {
                'hasUpdate': True,
                'localHash': local_hash[:7], 'remoteHash': remote_sha[:7],
                'message': commit_msg, 'downloadUrl': download_url
            }
    except Exception as e:
        # print(f"[AutoUpdate] 检查失败: {e}")
        pass
def _update_check_loop():
    """后台守护线程：启动后先等 10s 再首检，之后每 UPDATE_INTERVAL 检查一次"""
    time.sleep(10)
    while True:
        _do_update_check()
        time.sleep(UPDATE_INTERVAL)
# ── 辅助函数 ─────────────────────────────────────────────
def _json_ok(handler, data):
    body = json.dumps(data, ensure_ascii=False, indent=2).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

def _json_err(handler, code, msg):
    body = json.dumps({"error": msg}).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

def _read_body(handler):
    te = (handler.headers.get("Transfer-Encoding", "") or "").lower()
    if "chunked" in te:
        chunks = []
        while True:
            line = handler.rfile.readline()
            if not line:
                break
            size_hex = line.split(b";", 1)[0].strip()
            try:
                size = int(size_hex, 16)
            except Exception:
                break
            if size == 0:
                handler.rfile.readline()
                break
            chunk = handler.rfile.read(size)
            chunks.append(chunk)
            handler.rfile.read(2)
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length", 0))
    return handler.rfile.read(length) if length > 0 else b""

def _load_json_file(p):
    try:
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _atomic_write_json(p, data):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

def _scan_max_gen_seq_for_date(date_str):
    try:
        pat = re.compile(r"^gen_" + re.escape(date_str) + r"_(\d+)\.[a-z0-9]{1,5}$")
        max_n = 0
        for root, _, files in os.walk(OUTPUT_DIR):
            for fn in files:
                m = pat.match(fn)
                if not m:
                    continue
                try:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                except Exception:
                    continue
        return max_n
    except Exception:
        return 0

def _next_gen_output_filename(ext):
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    with _gen_seq_lock:
        state = _load_json_file(GEN_SEQ_STATE_FILE)
        last = 0
        try:
            last = int(state.get(date_str) or 0)
        except Exception:
            last = 0
        if last <= 0:
            scanned = _scan_max_gen_seq_for_date(date_str)
            if scanned > last:
                last = scanned
        n = last + 1
        state[date_str] = n
        try:
            _atomic_write_json(GEN_SEQ_STATE_FILE, state)
        except Exception:
            pass
    seq = str(n).zfill(4)
    return f"gen_{date_str}_{seq}.{ext}"


class Handler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    # 屏蔽日志噪音（按需注释掉）
    def log_message(self, fmt, *args):
        pass

    def send_head(self):
        path = self.translate_path(self.path)
        f = None
        if os.path.isdir(path):
            parts = urllib.parse.urlsplit(self.path)
            if not parts.path.endswith('/'):
                self.send_response(301)
                new_parts = (parts[0], parts[1], parts[2] + '/', parts[3], parts[4])
                new_url = urllib.parse.urlunsplit(new_parts)
                self.send_header("Location", new_url)
                self.end_headers()
                return None
            for index in ("index.html", "index.htm"):
                index_path = os.path.join(path, index)
                if os.path.exists(index_path):
                    path = index_path
                    break
            else:
                return self.list_directory(path)
        ctype = self.guess_type(path)
        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, "File not found")
            return None

        fs = os.fstat(f.fileno())
        size = fs.st_size
        range_header = self.headers.get("Range", "")
        self._range = None

        if range_header.startswith("bytes="):
            spec = range_header[6:].strip()
            if "," not in spec:
                start_s, dash, end_s = spec.partition("-")
                try:
                    if start_s == "":
                        suffix_len = int(end_s)
                        if suffix_len <= 0:
                            raise ValueError()
                        start = max(0, size - suffix_len)
                        end = size - 1
                    else:
                        start = int(start_s)
                        end = int(end_s) if end_s else size - 1
                    if start < 0 or start >= size:
                        raise ValueError()
                    end = min(end, size - 1)
                    if end < start:
                        raise ValueError()
                    self._range = (start, end)
                except Exception:
                    f.close()
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return None

        if self._range:
            start, end = self._range
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            f.seek(start)
            return f

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile):
        rng = getattr(self, "_range", None)
        if not rng:
            return super().copyfile(source, outputfile)
        start, end = rng
        remaining = end - start + 1
        bufsize = 64 * 1024
        while remaining > 0:
            chunk = source.read(min(bufsize, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    # ── OPTIONS 预检（CORS）──────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ════════════════════════════════════════════════════
    #  DELETE  /api/v2/projects/{filename}
    # ════════════════════════════════════════════════════
    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/v2/projects/"):
            fn = unquote(path[len("/api/v2/projects/"):])
            if fn and ".." not in fn and fn.endswith(".json"):
                fp = os.path.join(CANVAS_DIR, fn)
                if os.path.exists(fp):
                    os.remove(fp)
                    _json_ok(self, {"success": True})
                else:
                    _json_err(self, 404, "Project not found")
                return
                
        if path.startswith("/api/v2/assets/"):
            fn = unquote(path[len("/api/v2/assets/"):])
            if fn and ".." not in fn and fn.endswith(".json"):
                fp = os.path.join(ASSETS_DIR, fn)
                if os.path.exists(fp):
                    os.remove(fp)
                    _json_ok(self, {"success": True})
                else:
                    _json_err(self, 404, "Asset not found")
                return
                
        _json_err(self, 400, "Invalid request")

    # ════════════════════════════════════════════════════
    #  PATCH  /api/v2/projects/{filename}  → rename
    # ════════════════════════════════════════════════════
    def do_PATCH(self):
        import re
        path = self.path.split("?")[0]
        if path.startswith("/api/v2/projects/"):
            fn = unquote(path[len("/api/v2/projects/"):])
            if fn and ".." not in fn and fn.endswith(".json"):
                fp = os.path.join(CANVAS_DIR, fn)
                if not os.path.exists(fp):
                    _json_err(self, 404, "Project not found"); return
                body = _read_body(self)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    _json_err(self, 400, "Invalid JSON"); return
                new_name = data.get("name", "").strip()
                if not new_name:
                    _json_err(self, 400, "Name required"); return
                safe = re.sub(r'[\\/:*?"<>|]', "_", new_name)
                new_fn = safe + ".json"
                new_fp = os.path.join(CANVAS_DIR, new_fn)
                os.rename(fp, new_fp)
                _json_ok(self, {"success": True, "filename": new_fn})
                return
        _json_err(self, 400, "Invalid request")

    # ════════════════════════════════════════════════════
    #  GET
    # ════════════════════════════════════════════════════
    def do_GET(self):
        path = self.path.split("?")[0]

        # ── SSE 心跳长连接 ──
        if path == "/api/v2/heartbeat_stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"data: ping\n\n")
                    self.wfile.flush()
                    time.sleep(5)
            except Exception:
                # 客户端断开连接（刷新网页或关闭网页）
                pass
            return

        # ── 通用任务状态代理 (GET) ──
        if path == "/api/v2/proxy/task":
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            # 使用 keep_blank_values=False 和 max_num_fields=10 避免解析问题
            qs = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
            api_url = qs.get("apiUrl", [""])[0].strip() if "apiUrl" in qs else ""
            api_key = qs.get("apiKey", [""])[0].strip() if "apiKey" in qs else ""
            # 去除可能的逗号
            api_url = api_url.rstrip(',')
            api_key = api_key.rstrip(',')
            if not api_url or not api_key:
                _json_err(self, 400, "Missing apiUrl or apiKey"); return
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0"
            }
            try:
                # 尝试使用 requests (如果安装了)
                try:
                    import requests
                    resp = requests.get(api_url, headers=headers, timeout=30)
                    self.send_response(resp.status_code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp.content)
                    return
                except ImportError:
                    pass
                except Exception:
                    pass

                # 兜底使用 urllib
                import urllib.request, urllib.error
                req = urllib.request.Request(api_url, headers=headers, method="GET")
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        resp_data = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_data)
                except urllib.error.HTTPError as e:
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(e.read())
                except Exception as e:
                    _json_err(self, 500, f"Urllib polling error: {str(e)}")
            except Exception as e:
                _json_err(self, 500, f"Task proxy global error: {repr(e)}")
            return

        # ── 自动更新：检查接口 ──
        if path == "/api/v2/update/check":
            with _update_lock:
                info = _update_info
            if info:
                _json_ok(self, info)
            else:
                _json_ok(self, {'hasUpdate': False, 'localVersion': LOCAL_VERSION})
            return

        # ── 读取 API Key 配置 ──
        if path == "/api/config":
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                    try:
                        cfg = json.load(f)
                    except json.JSONDecodeError:
                        pass
            
            # 🔥 GRSAI API Key 环境变量智能保底逻辑 🔥
            env_grsai_key = os.environ.get("GRSAI_API_KEY", "").strip()
            if env_grsai_key:
                old_key = cfg.get("apiKey") or cfg.get("apiKeyInput")
                prov_grsai = cfg.get("providers", {}).get("grsai", {})
                new_key = prov_grsai.get("apiKey")
                
                # 当且仅当存 GRSAI key 的位置都为空时，才用环境变量注入内存配置，下发给前端
                if not old_key and not new_key:
                    if "providers" not in cfg:
                        cfg["providers"] = {}
                    if "grsai" not in cfg["providers"]:
                        cfg["providers"]["grsai"] = {}
                    cfg["providers"]["grsai"]["apiKey"] = env_grsai_key

            # 🔥 PPIO API Key 环境变量智能保底逻辑 🔥
            env_ppio_key = os.environ.get("PPIO_API_KEY", "").strip()
            if env_ppio_key:
                prov_ppio = cfg.get("providers", {}).get("ppio", {})
                new_key = prov_ppio.get("apiKey")
                
                # 当存 PPIO key 为空时，才用环境变量注入内存配置
                if not new_key:
                    if "providers" not in cfg:
                        cfg["providers"] = {}
                    if "ppio" not in cfg["providers"]:
                        cfg["providers"]["ppio"] = {}
                    cfg["providers"]["ppio"]["apiKey"] = env_ppio_key

            _json_ok(self, cfg)
            return

        # ── 自定义 AI 全局配置（GET）──
        if path == "/api/v2/config/custom-ai":
            cfg = _get_custom_ai_config()
            # apiKey 返回前两位 + 星号打码，防止泄露
            key = cfg["apiKey"]
            masked = key[:4] + "*" * (len(key) - 4) if len(key) > 4 else ("*" * len(key) if key else "")
            _json_ok(self, {"apiUrl": cfg["apiUrl"], "apiKeyMasked": masked, "hasKey": bool(key), "source": cfg["source"]})
            return

        # ── 列出画布项目 ──
        if path == "/api/v2/projects":
            files = []
            for fn in os.listdir(CANVAS_DIR):
                if not fn.endswith(".json"):
                    continue
                fp = os.path.join(CANVAS_DIR, fn)
                files.append({
                    "filename": fn,
                    "name":     fn[:-5],
                    "mtime":    os.path.getmtime(fp),
                })
            files.sort(key=lambda x: x["mtime"], reverse=True)
            _json_ok(self, files)
            return

        # ── 加载指定画布项目 ──
        if path.startswith("/api/v2/projects/") and not path.endswith("/save"):
            fn = unquote(path[len("/api/v2/projects/"):])
            if fn and ".." not in fn:
                fp = os.path.join(CANVAS_DIR, fn)
                if os.path.exists(fp):
                    with open(fp, "r", encoding="utf-8-sig") as f:
                        _json_ok(self, json.load(f))
                else:
                    _json_err(self, 404, "Project not found")
                return

        # ── 资产数据接口 ──
        if path == "/api/v2/assets":
            files = []
            if os.path.exists(ASSETS_DIR):
                for fn in os.listdir(ASSETS_DIR):
                    if not fn.endswith(".json"): continue
                    fp = os.path.join(ASSETS_DIR, fn)
                    try:
                        with open(fp, "r", encoding="utf-8-sig") as f:
                            data = json.load(f)
                            if isinstance(data, dict) and not data.get("id"):
                                data["id"] = fn[:-5]
                            files.append(data)
                    except Exception:
                        pass
            _json_ok(self, files)
            return

        # ── 读取用户配置文件 ──
        if path.startswith("/api/v2/user/") and not path.startswith("/api/v2/user/presets"):
            fn = path[len("/api/v2/user/"):]
            if fn and fn.endswith(".json") and "/" not in fn and ".." not in fn:
                fp = os.path.join(USER_DIR, fn)
                if os.path.exists(fp):
                    with open(fp, "r", encoding="utf-8-sig") as f:
                        _json_ok(self, json.load(f))
                else:
                    _json_ok(self, {})   # 首次访问返回空对象
                return

        # ── 获取基于文件夹TXT的自定义提示词预设 ──
        if path == "/api/v2/user/presets":
            prompt_dir = os.path.join(USER_DIR, "prompt")
            # 确保初始的四个分类文件夹存在，并生成示例
            default_types = ["ai-image", "ai-text", "ai-video", "ai-audio"]
            for t in default_types:
                t_dir = os.path.join(prompt_dir, t)
                if not os.path.exists(t_dir):
                    os.makedirs(t_dir, exist_ok=True)

            # 遍历结构构建预设字典
            result = {}
            if os.path.exists(prompt_dir):
                for node_type in os.listdir(prompt_dir):
                    t_dir = os.path.join(prompt_dir, node_type)
                    if os.path.isdir(t_dir):
                        result[node_type] = []
                        for fn in os.listdir(t_dir):
                            if fn.endswith(".txt"):
                                fp = os.path.join(t_dir, fn)
                                try:
                                    with open(fp, "r", encoding="utf-8") as f:
                                        content = f.read().strip()
                                        if content:
                                            result[node_type].append({
                                                "title": fn[:-4], # 去除 .txt 扩展名
                                                "template": content
                                            })
                                except Exception as e:
                                    print(f"Error reading preset {fp}: {e}")
            _json_ok(self, result)
            return

        # ── 其余静态文件（由 SimpleHTTPRequestHandler 处理）──
        try:
            super().do_GET()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    # ════════════════════════════════════════════════════
    #  POST
    # ════════════════════════════════════════════════════
    def do_POST(self):
        path = self.path.split("?")[0]

        # ── 保存 API Key 配置 ──
        if path == "/api/config":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON")
                return
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _json_ok(self, {"success": True})
            return

        # ── 保存画布项目 ──
        if path == "/api/v2/projects/save":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON")
                return

            name = data.get("projectName", "未命名画布").strip() or "未命名画布"
            # 文件名安全化：只保留中文、字母、数字、空格、横杠
            safe = re.sub(r'[\\/:*?"<>|]', "_", name)
            fname = safe + ".json"
            fpath = os.path.join(CANVAS_DIR, fname)

            # 兼容 V1 和 V2 格式：V2 会传来 canvases 和 activeCanvasId，V1 原生传来 nodes 和 edges
            payload = {}
            if "canvases" in data:
                payload["canvases"] = data["canvases"]
                payload["activeCanvasId"] = data.get("activeCanvasId", "canvas_1")
            else:
                payload["nodes"] = data.get("nodes", {})
                payload["edges"] = data.get("edges", {})
                payload["viewport"] = data.get("viewport", {})
                
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            _json_ok(self, {"success": True, "filename": fname})
            return

        # ── 保存单个资产 ──
        if path == "/api/v2/assets/save":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON")
                return
            
            asset_id = data.get("id")
            if not asset_id:
                _json_err(self, 400, "Asset ID required")
                return
                
            safe_id = re.sub(r'[\\/:*?"<>|]', "_", str(asset_id))
            fname = safe_id + ".json"
            fpath = os.path.join(ASSETS_DIR, fname)
            
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
            _json_ok(self, {"success": True, "id": asset_id})
            return

        # ── 保存资产缩略图（data/assets/thumbs） ──
        if path == "/api/v2/assets/thumb/save":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON")
                return

            asset_id = data.get("assetId") or data.get("id")
            key = data.get("key") or data.get("idx") or "0"
            data_url = data.get("dataUrl") or ""

            if not asset_id:
                _json_err(self, 400, "Asset ID required")
                return
            if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
                _json_err(self, 400, "Invalid dataUrl")
                return

            try:
                header, b64 = data_url.split(",", 1)
            except Exception:
                _json_err(self, 400, "Invalid dataUrl")
                return

            mime = "image/jpeg"
            try:
                mime = header[5:].split(";", 1)[0]
            except Exception:
                pass

            ext = ".jpg"
            if mime.endswith("png"):
                ext = ".png"
            elif mime.endswith("webp"):
                ext = ".webp"

            safe_id = re.sub(r'[\\/:*?"<>|]', "_", str(asset_id))
            safe_key = re.sub(r'[\\/:*?"<>|]', "_", str(key))
            fname = f"{safe_id}_{safe_key}{ext}"
            fpath = os.path.join(ASSET_THUMBS_DIR, fname)

            try:
                raw = base64.b64decode(b64)
            except Exception:
                _json_err(self, 400, "Invalid base64")
                return

            with open(fpath, "wb") as f:
                f.write(raw)

            rel_url = f"/data/assets/thumbs/{fname}"
            _json_ok(self, {"success": True, "url": rel_url, "localPath": f"data/assets/thumbs/{fname}", "filename": fname})
            return

        # ── 写入用户配置文件 ──
        if path.startswith("/api/v2/user/"):
            fn = path[len("/api/v2/user/"):]
            if not fn or not fn.endswith(".json") or "/" in fn or ".." in fn:
                _json_err(self, 400, "Invalid filename")
                return
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON")
                return
            fp = os.path.join(USER_DIR, fn)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _json_ok(self, {"success": True})
            return

        # ── 文件上传 ──
        if path == "/api/upload":
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                content_type = self.headers.get("Content-Type", "") or ""
                body = _read_body(self)

                filename = (qs.get("filename", [""])[0] or "").strip()
                file_bytes = body

                if content_type.startswith("multipart/form-data") and b"\r\n" in body:
                    m = re.search(r'boundary=([^;]+)', content_type)
                    boundary = (m.group(1).strip().strip('"') if m else "")
                    if boundary:
                        boundary_bytes = ("--" + boundary).encode("utf-8", "ignore")
                        parts = body.split(boundary_bytes)
                        for part in parts:
                            if b'Content-Disposition:' not in part:
                                continue
                            if b'name="file"' not in part and b"name='file'" not in part:
                                continue
                            header_end = part.find(b"\r\n\r\n")
                            if header_end == -1:
                                continue
                            header_blob = part[:header_end].decode("utf-8", "ignore")
                            data_blob = part[header_end + 4 :]
                            if data_blob.endswith(b"\r\n"):
                                data_blob = data_blob[:-2]
                            if data_blob.endswith(b"--"):
                                data_blob = data_blob[:-2]
                            if not filename:
                                mf = re.search(r'filename="([^"]+)"', header_blob)
                                if mf:
                                    filename = mf.group(1).strip()
                            file_bytes = data_blob
                            break

                if not filename:
                    filename = "upload"

                safe_fn = re.sub(r'[\\/:*?"<>|]', "_", os.path.basename(filename))
                fpath = os.path.join(UPLOADS_DIR, safe_fn)
                with open(fpath, "wb") as f:
                    f.write(file_bytes)
                rel_url = f"/data/uploads/{safe_fn}"
                _json_ok(self, {"url": rel_url, "localPath": f"data/uploads/{safe_fn}", "filename": safe_fn})
            except Exception as e:
                try:
                    _json_err(self, 500, f"Upload failed: {str(e)}")
                except Exception:
                    pass
            return

        # ── 静默本地硬备份 (前端生成后调用保存到 output) ──
        if path == "/api/v2/save_output":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ext = (qs.get("ext", ["png"])[0] or "png").strip().lower()
            if not re.match(r"^[a-z0-9]{1,5}$", ext):
                ext = "png"
            
            # 支持指定子目录
            sub_dir = (qs.get("subDir", [""])[0] or "").strip()
            if sub_dir and re.match(r"^[a-zA-Z0-9_-]+$", sub_dir):
                target_dir = os.path.join(OUTPUT_DIR, sub_dir)
                os.makedirs(target_dir, exist_ok=True)
                filename = _next_gen_output_filename(ext)
                fpath = os.path.join(target_dir, filename)
                rel_path = f"output/{sub_dir}/{filename}"
            else:
                filename = _next_gen_output_filename(ext)
                fpath = os.path.join(OUTPUT_DIR, filename)
                rel_path = f"output/{filename}"
            
            body = _read_body(self)
            if body:
                with open(fpath, "wb") as f:
                    f.write(body)
                _json_ok(
                    self,
                    {
                        "success": True,
                        "filename": filename,
                        "path": rel_path,
                        "localPath": rel_path,
                        "url": f"/{rel_path}",
                    },
                )
            else:
                _json_err(self, 400, "Empty payload")
            return

        # ── 视频裁剪 (依赖 FFmpeg) ──
        if path.rstrip("/") == "/api/v2/video/cut":
            import time
            import random
            import subprocess
            body = _read_body(self)
            try:
                data = json.loads(body or b"{}")
            except Exception:
                _json_err(self, 400, "Invalid JSON")
                return
            
            src_path = (data.get("src") or "").strip()
            start_sec = float(data.get("start", 0))
            end_sec = float(data.get("end", 0))
            
            if not src_path or end_sec <= start_sec:
                _json_err(self, 400, "Invalid parameters")
                return
            
            # 安全处理源路径：去除前导斜杠，转换为本地绝对路径（基于 DIRECTORY）
            safe_src = src_path.lstrip("/")
            norm_src = os.path.normpath(safe_src)
            if norm_src.startswith("..") or norm_src.startswith("../") or norm_src.startswith("..\\"):
                _json_err(self, 400, "Invalid src path")
                return
            local_src = os.path.join(DIRECTORY, norm_src)
            
            if not os.path.exists(local_src):
                _json_err(self, 404, "Source video not found")
                return
                
            # 准备输出目录
            cut_dir = os.path.join(OUTPUT_DIR, "CutVideo")
            os.makedirs(cut_dir, exist_ok=True)
            
            ts = int(time.time() * 1000)
            rand_str = f"{random.randint(100,999)}"
            filename = f"cut_{ts}_{rand_str}.mp4"
            out_path = os.path.join(cut_dir, filename)
            
            try:
                # 使用 FFmpeg 进行精准裁剪 (-ss 放在输入前可以加速，放在输入后可以更精准，这里用精确模式)
                # 重新编码以保证兼容性和精准切点，或者使用 copy 快速切（可能不准）
                # 这里为了稳定，我们使用重新编码
                cmd = [
                    "ffmpeg", "-y",
                    "-i", local_src,
                    "-ss", str(start_sec),
                    "-t", str(end_sec - start_sec),
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-c:a", "aac",
                    out_path
                ]
                
                # 隐藏控制台窗口 (Windows)
                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    startupinfo=startupinfo
                )
                stdout, stderr = process.communicate(timeout=120)
                
                if process.returncode != 0:
                    print(f"FFmpeg error: {stderr.decode('utf-8', errors='ignore')}")
                    _json_err(self, 500, "FFmpeg processing failed")
                    return
                    
                _json_ok(self, {
                    "success": True, 
                    "filename": filename, 
                    "path": f"output/CutVideo/{filename}",
                    "localPath": f"output/CutVideo/{filename}",
                    "url": f"/output/CutVideo/{filename}",
                })
            except subprocess.TimeoutExpired:
                process.kill()
                _json_err(self, 504, "FFmpeg process timeout")
            except Exception as e:
                _json_err(self, 500, f"Error processing video: {str(e)}")
            return

        # ── 视频元数据 (依赖 FFprobe/FFmpeg) ──
        if path.rstrip("/") == "/api/v2/video/meta":
            import subprocess
            body = _read_body(self)
            try:
                data = json.loads(body or b"{}")
            except Exception:
                _json_err(self, 400, "Invalid JSON")
                return

            src_path = (data.get("src") or "").strip()
            if not src_path:
                _json_err(self, 400, "Missing src")
                return

            safe_src = src_path.lstrip("/")
            norm_src = os.path.normpath(safe_src)
            if norm_src.startswith("..") or norm_src.startswith("../") or norm_src.startswith("..\\"):
                _json_err(self, 400, "Invalid src path")
                return
            local_src = os.path.join(DIRECTORY, norm_src)

            if not os.path.exists(local_src):
                _json_err(self, 404, "Source video not found")
                return

            def _parse_ratio(s):
                try:
                    raw = (s or "").strip()
                    if not raw:
                        return 0.0
                    if "/" in raw:
                        a, b = raw.split("/", 1)
                        na = float(a)
                        nb = float(b)
                        if nb == 0:
                            return 0.0
                        return na / nb
                    return float(raw)
                except Exception:
                    return 0.0

            try:
                cmd = [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "format=duration:stream=avg_frame_rate,r_frame_rate,nb_frames,duration",
                    "-of",
                    "json",
                    local_src,
                ]

                startupinfo = None
                if os.name == "nt":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    startupinfo=startupinfo,
                )
                stdout, stderr = process.communicate(timeout=20)
                if process.returncode != 0:
                    err_text = (stderr or b"").decode("utf-8", errors="ignore").strip()
                    _json_err(self, 500, f"FFprobe failed: {err_text or 'unknown error'}")
                    return

                try:
                    meta = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
                except Exception:
                    meta = {}

                streams = meta.get("streams") or []
                s0 = streams[0] if streams else {}
                fmt = meta.get("format") or {}

                duration = 0.0
                for k in ("duration",):
                    v = fmt.get(k)
                    try:
                        dv = float(v)
                        if dv > 0:
                            duration = dv
                            break
                    except Exception:
                        pass
                if duration <= 0:
                    try:
                        dv = float(s0.get("duration") or 0)
                        if dv > 0:
                            duration = dv
                    except Exception:
                        pass

                fps = _parse_ratio(s0.get("avg_frame_rate") or "") or _parse_ratio(
                    s0.get("r_frame_rate") or "",
                )
                if fps <= 0:
                    fps = 0.0

                frame_count = 0
                nb_frames = s0.get("nb_frames")
                try:
                    if nb_frames is not None:
                        frame_count = int(float(nb_frames))
                except Exception:
                    frame_count = 0
                if frame_count <= 0 and fps > 0 and duration > 0:
                    frame_count = int(round(duration * fps))

                _json_ok(
                    self,
                    {
                        "success": True,
                        "fps": fps if fps > 0 else None,
                        "frameCount": frame_count if frame_count > 0 else None,
                        "duration": duration if duration > 0 else None,
                    },
                )
            except subprocess.TimeoutExpired:
                process.kill()
                _json_err(self, 504, "FFprobe process timeout")
            except Exception as e:
                _json_err(self, 500, f"Error reading video meta: {str(e)}")
            return

        # ── 视频首帧缩略图（依赖 FFmpeg，产物落盘到 output/VideoThumbs） ──
        if path.rstrip("/") == "/api/v2/video/first_frame":
            import subprocess
            body = _read_body(self)
            try:
                data = json.loads(body or b"{}")
            except Exception:
                _json_err(self, 400, "Invalid JSON")
                return

            src_path = (data.get("src") or "").strip()
            if not src_path:
                _json_err(self, 400, "Missing src")
                return

            # 安全处理源路径：去除前导斜杠，转换为本地绝对路径（基于 DIRECTORY）
            safe_src = src_path.lstrip("/")
            norm_src = os.path.normpath(safe_src)
            if norm_src.startswith("..") or norm_src.startswith("../") or norm_src.startswith("..\\"):
                _json_err(self, 400, "Invalid src path")
                return
            local_src = os.path.join(DIRECTORY, norm_src)

            if not os.path.exists(local_src):
                _json_err(self, 404, "Source video not found")
                return

            try:
                st = os.stat(local_src)
            except Exception:
                _json_err(self, 500, "Cannot stat source video")
                return

            # 用“源路径 + mtime + size”生成稳定文件名，避免重复生成导致 output 膨胀
            sig = f"{norm_src}|{getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))}|{st.st_size}"
            h = hashlib.sha1(sig.encode("utf-8", errors="ignore")).hexdigest()[:12]

            thumb_dir = os.path.join(OUTPUT_DIR, "VideoThumbs")
            os.makedirs(thumb_dir, exist_ok=True)
            filename = f"vthumb_{h}.jpg"
            out_path = os.path.join(thumb_dir, filename)

            if not os.path.exists(out_path):
                try:
                    # 取 0 秒处首帧并缩放到 320px 宽（等比），生成 jpg 以控制文件体积
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-ss",
                        "0",
                        "-i",
                        local_src,
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=320:-1",
                        "-q:v",
                        "4",
                        "-an",
                        out_path,
                    ]

                    startupinfo = None
                    if os.name == "nt":
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        startupinfo=startupinfo,
                    )
                    stdout, stderr = process.communicate(timeout=30)
                    if process.returncode != 0:
                        print(
                            f"FFmpeg first_frame error: {(stderr or b'').decode('utf-8', errors='ignore')}"
                        )
                        _json_err(self, 500, "FFmpeg processing failed")
                        return
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    _json_err(self, 504, "FFmpeg process timeout")
                    return
                except Exception as e:
                    _json_err(self, 500, f"Error extracting first frame: {str(e)}")
                    return

            rel_path = f"output/VideoThumbs/{filename}"
            _json_ok(self, {"success": True, "url": "/" + rel_path, "localPath": rel_path})
            return

        # ── 从远端 URL 下载并保存到 output（用于视频等二进制产物落盘） ──
        if path == "/api/v2/save_output_from_url":
            import socket
            import ipaddress
            import urllib.parse
            import urllib.request
            import urllib.error
            body = _read_body(self)
            try:
                data = json.loads(body or b"{}")
            except Exception:
                _json_err(self, 400, "Invalid JSON")
                return
            url = (data.get("url") or "").strip()
            if not url:
                _json_err(self, 400, "Missing url")
                return
            if url.startswith("//"):
                url = "https:" + url
            elif not re.match(r"^https?://", url, flags=re.I):
                if (
                    ".myqcloud.com" in url.lower()
                    or ".qcloud.com" in url.lower()
                    or ".runninghub.cn" in url.lower()
                    or "runninghub.cn/" in url.lower()
                ):
                    url = "https://" + url.lstrip("/")
            try:
                parsed = urllib.parse.urlparse(url)
            except Exception:
                _json_err(self, 400, "Invalid url")
                return
            if parsed.scheme not in ("http", "https"):
                _json_err(self, 400, "Only http/https url allowed")
                return
            host = parsed.hostname
            if not host:
                _json_err(self, 400, "Invalid host")
                return

            def _is_allowlisted_download_host(h):
                try:
                    hh = (h or "").strip().lower().strip(".")
                except Exception:
                    return False
                if not hh:
                    return False
                if hh == "runninghub.cn" or hh.endswith(".runninghub.cn"):
                    return True
                if hh.endswith(".myqcloud.com") or hh.endswith(".qcloud.com"):
                    return True
                return False

            def _is_private_ip(ip_str):
                try:
                    ip = ipaddress.ip_address(ip_str)
                except Exception:
                    return True
                return (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
                    or ip.is_unspecified
                )

            try:
                allow_private = _is_allowlisted_download_host(host)
                if not allow_private:
                    infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
                    for info in infos:
                        ip_str = info[4][0]
                        if _is_private_ip(ip_str):
                            _json_err(self, 400, "Blocked private/reserved address")
                            return
            except Exception:
                _json_err(self, 400, "DNS resolve failed")
                return

            max_bytes = int(data.get("maxBytes") or 1024 * 1024 * 300)

            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "AI-Canvas/1.0")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    ext = (data.get("ext") or "").strip().lower()
                    if not re.match(r"^[a-z0-9]{1,5}$", ext):
                        ext = ""
                    if not ext:
                        if ct == "video/mp4":
                            ext = "mp4"
                        elif ct in ("video/webm", "audio/webm"):
                            ext = "webm"
                        else:
                            ext = "bin"
                    filename = _next_gen_output_filename(ext)
                    fpath = os.path.join(OUTPUT_DIR, filename)
                    total = 0
                    with open(fpath, "wb") as f:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_bytes:
                                try:
                                    os.remove(fpath)
                                except Exception:
                                    pass
                                _json_err(self, 413, "File too large")
                                return
                            f.write(chunk)
            except urllib.error.HTTPError as e:
                _json_err(self, 502, f"Download HTTPError: {e.code}")
                return
            except Exception as e:
                _json_err(self, 502, f"Download failed: {str(e)}")
                return

            rel_path = f"output/{filename}"
            _json_ok(
                self,
                {
                    "success": True,
                    "filename": filename,
                    "path": rel_path,
                    "localPath": rel_path,
                    "url": f"/{rel_path}",
                },
            )
            return

        # ── 自定义 AI 全局配置（POST）──
        if path == "/api/v2/config/custom-ai":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            # 如果当前是环境变量来源，拒绝覆盖
            conf = _get_custom_ai_config()
            if conf["source"] == "env":
                _json_err(self, 403, "Config is locked by environment variables (CUSTOM_AI_URL / CUSTOM_AI_KEY)"); return
            # 写入 config.json
            try:
                existing = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, encoding="utf-8-sig") as f:
                        existing = json.load(f)
                existing["custom_ai"] = {"apiUrl": data.get("apiUrl", "").strip(), "apiKey": data.get("apiKey", "").strip()}
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
                _json_ok(self, {"success": True})
            except Exception as e:
                _json_err(self, 500, str(e))
            return

        # ── 文件上传代理（RunningHUB 等）──
        if path == "/api/v2/proxy/upload":
            try:
                import urllib.request
                import urllib.error
                
                # 从查询参数获取 apiUrl 和 apiKey
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                api_url = qs.get("apiUrl", [""])[0].strip()
                api_key = qs.get("apiKey", [""])[0].strip()
                
                if not api_url or not api_key:
                    _json_err(self, 400, "Missing apiUrl or apiKey"); return
                
                # 读取原始请求体（multipart/form-data）
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                content_type = self.headers.get('Content-Type', '')
                
                # 直接转发到 RunningHUB
                req = urllib.request.Request(api_url, data=body, method="POST")
                req.add_header("Authorization", f"Bearer {api_key}")
                req.add_header("Content-Type", content_type)
                req.add_header("Content-Length", str(len(body)))
                
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp_body = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
                return
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(e.read())
                return
            except Exception as e:
                _json_err(self, 500, f"Upload proxy error: {str(e)}")
                return

        if path == "/api/v2/runninghubwf/run":
            body = _read_body(self)
            try:
                data = json.loads(body)
                api_key = (data.get("apiKey") or "").strip()
                workflow_id = str(data.get("workflowId") or "").strip()
                node_info_list = data.get("nodeInfoList")
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            if not api_key or not workflow_id or not isinstance(node_info_list, list):
                _json_err(self, 400, "Missing apiKey or workflowId or nodeInfoList"); return

            api_url = "https://www.runninghub.cn/task/openapi/create"
            instance_type = data.get("instanceType") or data.get("rhInstanceType") or ""
            instance_type = str(instance_type).strip().lower()
            if instance_type in ("24g", "default", "basic"):
                instance_type = "default"
            elif instance_type in ("48g", "plus", "pro"):
                instance_type = "plus"
            else:
                instance_type = "default"
            payload = dict(data)
            payload["instanceType"] = instance_type
            try:
                import requests as _req
                resp = _req.post(api_url, json=payload, timeout=900)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp.content)
            except ImportError:
                import urllib.request, urllib.error
                req_body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(api_url, data=req_body, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", "Mozilla/5.0")
                try:
                    with urllib.request.urlopen(req, timeout=900) as resp:
                        resp_data = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_data)
                except urllib.error.HTTPError as e:
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(e.read())
            except Exception as e:
                _json_err(self, 500, f"RunningHub workflow proxy error: {repr(e)}")
            return

        if path == "/api/v2/runninghubwf/query":
            body = _read_body(self)
            try:
                data = json.loads(body)
                api_key = (data.get("apiKey") or "").strip()
                task_id = str(data.get("taskId") or "").strip()
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            if not api_key or not task_id:
                _json_err(self, 400, "Missing apiKey or taskId"); return

            api_url = "https://www.runninghub.cn/task/openapi/outputs"
            payload = { "apiKey": api_key, "taskId": task_id }
            try:
                import requests as _req
                resp = _req.post(api_url, json=payload, timeout=60)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp.content)
            except ImportError:
                import urllib.request, urllib.error
                req_body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(api_url, data=req_body, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", "Mozilla/5.0")
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        resp_data = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_data)
                except urllib.error.HTTPError as e:
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(e.read())
            except Exception as e:
                _json_err(self, 500, f"RunningHub query proxy error: {repr(e)}")
            return

        if path == "/api/v2/runninghubwf/cancel":
            body = _read_body(self)
            try:
                data = json.loads(body)
                api_key = (data.get("apiKey") or "").strip()
                task_id = str(data.get("taskId") or "").strip()
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            if not api_key or not task_id:
                _json_err(self, 400, "Missing apiKey or taskId"); return

            api_url = "https://www.runninghub.cn/task/openapi/cancel"
            payload = { "apiKey": api_key, "taskId": task_id }
            try:
                import requests as _req
                resp = _req.post(api_url, json=payload, timeout=60)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp.content)
            except ImportError:
                import urllib.request, urllib.error
                req_body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(api_url, data=req_body, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", "Mozilla/5.0")
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        resp_data = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_data)
                except urllib.error.HTTPError as e:
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(e.read())
            except Exception as e:
                _json_err(self, 500, f"RunningHub cancel proxy error: {repr(e)}")
            return

        # ── PPIO 图像生成代理 ──
        if path == "/api/v2/proxy/image":
            body = _read_body(self)
            try:
                data = json.loads(body)
                api_url = data.pop("apiUrl", "").strip().rstrip("/")
                api_key = data.pop("apiKey", "").strip()
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            if not api_url or not api_key:
                _json_err(self, 400, "Missing apiUrl or apiKey"); return
            
            # ── 通用图像生成代理 ──
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0"
            }
            try:
                import requests as _req
                resp = _req.post(api_url, json=data, headers=headers, timeout=900)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp.content)
            except ImportError:
                import urllib.request, urllib.error
                req_body = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(api_url, data=req_body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=900) as resp:
                        resp_data = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_data)
                except urllib.error.HTTPError as e:
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(e.read())
            except Exception as e:
                _json_err(self, 500, f"Proxy error: {repr(e)}")
            return

        # ── 通用代理 forwarded ──
        if path == "/api/v2/proxy/completions":
            body = _read_body(self)
            try:
                data = json.loads(body)
                api_url = data.pop("apiUrl", "").strip().rstrip("/")
                api_key = data.pop("apiKey", "").strip()
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            
            if not api_url or not api_key:
                global_cfg = _get_custom_ai_config()
                api_url = api_url or global_cfg["apiUrl"]
                api_key = api_key or global_cfg["apiKey"]

            if not api_url or not api_key:
                _json_err(self, 400, "Missing apiUrl or apiKey"); return
            
            # 兼容 Gemini 格式端点或已完整的端点
            if ":generateContent" in api_url or "/v1beta/models" in api_url or api_url.endswith("/chat/completions"):
                endpoint = api_url
            else:
                endpoint = f"{api_url}/chat/completions"
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/json"
            }
            
            try:
                import requests
                req_body = json.dumps(data)
                try:
                    # 超时时间改为 300 秒（5分钟），与前端 aiTextApi.js 保持一致
                    resp = requests.post(endpoint, data=req_body, headers=headers, timeout=300)
                except requests.exceptions.ConnectionError as ce:
                    _json_err(self, 502, f"无法连接到 AI 服务器: {str(ce)}")
                    return
                except requests.exceptions.Timeout as te:
                    _json_err(self, 504, f"AI 服务器请求超时: {str(te)}")
                    return
                except requests.exceptions.RequestException as req_err:
                    _json_err(self, 502, f"AI 服务器请求失败: {str(req_err)}")
                    return
                
                # 检查响应是否是 SSE 格式，如果是则提取有效 JSON
                resp_text = resp.text
                resp_content_type = resp.headers.get('Content-Type', '')
                
                # 如果响应包含 text/event-stream 或多行 data: 格式，尝试提取 JSON
                is_sse = 'text/event-stream' in resp_content_type or resp_text.strip().startswith('data:')
                if is_sse:
                    try:
                        # 尝试从 SSE 格式中提取有效 JSON
                        lines = [l.strip() for l in resp_text.split('\n') if l.strip().startswith('data:')]
                        if lines:
                            last_line = lines[-1].replace('data:', '').strip()
                            if last_line == '[DONE]':
                                # 找倒数第二个有效行
                                valid_lines = [l for l in lines if l.replace('data:', '').strip() != '[DONE]']
                                if valid_lines:
                                    json_str = valid_lines[-1].replace('data:', '').strip()
                                    json_data = json.loads(json_str)
                                    resp_text = json.dumps(json_data)
                            else:
                                json_data = json.loads(last_line)
                                resp_text = json.dumps(json_data)
                    except Exception:
                        # 如果解析失败，保持原样返回
                        pass
                
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_text.encode('utf-8'))
            except ImportError:
                # Fallback to urllib if requests is not installed
                import urllib.request
                req_body = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(endpoint, data=req_body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        resp_data = resp.read()
                        resp_text = resp_data.decode('utf-8')
                    
                    # 检查响应是否是 SSE 格式，如果是则提取有效 JSON
                    if resp_text.strip().startswith('data:'):
                        try:
                            lines = [l.strip() for l in resp_text.split('\n') if l.strip().startswith('data:')]
                            if lines:
                                last_line = lines[-1].replace('data:', '').strip()
                                if last_line == '[DONE]':
                                    valid_lines = [l for l in lines if l.replace('data:', '').strip() != '[DONE]']
                                    if valid_lines:
                                        json_str = valid_lines[-1].replace('data:', '').strip()
                                        json_data = json.loads(json_str)
                                        resp_text = json.dumps(json_data)
                                else:
                                    json_data = json.loads(last_line)
                                    resp_text = json.dumps(json_data)
                        except Exception:
                            pass

                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_text.encode('utf-8'))
                except urllib.error.HTTPError as e:
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(e.read())
            except Exception as e:
                _json_err(self, 500, repr(e))
            return

        # ── 自定义 AI 文本生成（代理转发 OpenAI 格式请求）──
        if path == "/api/v2/chat":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            api_url  = data.get("apiUrl", "").strip().rstrip("/")
            api_key  = data.get("apiKey", "").strip()
            model    = data.get("model", "")
            prompt   = data.get("prompt", "")
            # apiKey 留空时，读取环境变量 CUSTOM_AI_KEY 或 config.json（apiUrl 必须由节点提供）
            if not api_key:
                global_cfg = _get_custom_ai_config()
                api_key = global_cfg["apiKey"]
            if not api_url or not api_key or not model or not prompt:
                _json_err(self, 400, "Missing required fields: apiUrl, apiKey, model, prompt"); return
            
            # 判断端点拼接，如果本身就是直接写完到了 completion 就不拼接了
            endpoint = api_url if api_url.endswith("/chat/completions") else f"{api_url}/chat/completions"
            
            import urllib.request
            req_body = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}]
            }).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=req_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))

                content = resp_data["choices"][0]["message"]["content"]
                _json_ok(self, {"content": content})
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                try: err_msg = json.loads(err_body).get("error", {}).get("message", err_body)
                except: err_msg = err_body
                _json_err(self, e.code, err_msg)
            except Exception as e:
                _json_err(self, 500, str(e))
            return

        # ── 自动更新：git pull 后启动 双击运行.bat 并退出当前进程 ──
        if path == "/api/v2/update/apply":
            try:
                # ZIP 包内的 .git 由 CI 生成；远端可能被 --force 推送导致历史重写
                # 这里不使用 git pull（merge），而是采用 fetch + reset 硬对齐远端版本，避免冲突/无追踪分支等问题
                remotes = []
                try:
                    remotes = subprocess.check_output(
                        ['git', 'remote'],
                        cwd=DIRECTORY, stderr=subprocess.DEVNULL
                    ).decode().split()
                except Exception:
                    remotes = []
                remote = None
                for name in ("origin", "github", "gitee"):
                    if name in remotes:
                        remote = name
                        break
                if not remote and remotes:
                    remote = remotes[0]
                if not remote:
                    _json_ok(self, {'success': False, 'error': '未检测到可用的 git remote（可能不是通过 Git 获取的目录）'})
                    return

                fetch = subprocess.run(
                    ['git', 'fetch', remote, 'master'],
                    cwd=DIRECTORY,
                    capture_output=True, text=True, timeout=60
                )
                if fetch.returncode != 0:
                    err = fetch.stderr.strip() or fetch.stdout.strip()
                    _json_ok(self, {'success': False, 'error': err})
                    return
                reset = subprocess.run(
                    ['git', 'reset', '--hard', 'FETCH_HEAD'],
                    cwd=DIRECTORY,
                    capture_output=True, text=True, timeout=60
                )
                if reset.returncode == 0:
                    _json_ok(self, {'success': True})
                    def _restart():
                        import time, os
                        time.sleep(0.8)
                        bat = os.path.join(DIRECTORY, '双击运行.bat')
                        os.startfile(bat)
                        time.sleep(0.3)
                        os._exit(0)
                    threading.Thread(target=_restart, daemon=True).start()
                else:
                    err = reset.stderr.strip() or reset.stdout.strip()
                    _json_ok(self, {'success': False, 'error': err})
            except subprocess.TimeoutExpired:
                _json_err(self, 504, 'git pull 超时，请检查网络')
            except Exception as e:
                _json_err(self, 500, str(e))
            return
        _json_err(self, 404, "Not found")


# ── 启动 ─────────────────────────────────────────────────
if __name__ == "__main__":
    # 启动自动更新后台检查线程
    _t = threading.Thread(target=_update_check_loop, daemon=True, name='AutoUpdateChecker')
    _t.start()
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except Exception:
            port = PORT
    with socketserver.ThreadingTCPServer(("", port), Handler) as httpd:
        httpd.allow_reuse_address = True
        print(f"╔══════════════════════════════════════╗")
        print(f"║  AI Canvas 服务器已启动              ║")
        print(f"║  http://localhost:{port}              ║")
        print(f"║  按 Ctrl+C 停止服务器                ║")
        print(f"╚══════════════════════════════════════╝")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务器已停止。")
