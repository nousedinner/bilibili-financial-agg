"""B站 API 客户端 — WBI签名、视频列表、动态列表、反限速。"""

import asyncio
import hashlib
import json as _json
import subprocess
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import httpx

from src.config import get_config, get_env


class BiliAPIError(Exception):
    """B站 API 返回错误码时抛出（code != 0）。"""

    def __init__(self, code: int, message: str, result: dict | None = None):
        super().__init__(f"BiliAPIError(code={code}, msg={message})")
        self.code = code
        self.message = message
        self.result = result or {}


# ------------------------------------------------------------------
# curl 统一请求（含超时回收）
# ------------------------------------------------------------------

async def _run_curl(
    args: list[str],
    timeout: float = 20,
    sep_body_status: bool = False,
) -> tuple[bytes, int]:
    """执行 curl 子进程并返回 (stdout_bytes, http_status_code)。

    - timeout 超时后会 terminate 进程并等待回收，防止僵尸进程。
    - sep_body_status=True 时，用 -w '\\n%{http_code}' 分离 body 和状态码。
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"curl timed out after {timeout}s: {' '.join(args[:3])}")

    status = 0
    body = stdout
    if sep_body_status and stdout:
        text = stdout.decode("utf-8", errors="replace")
        lines = text.rsplit("\n", 1)
        if len(lines) >= 2:
            body = lines[0].encode()
            try:
                status = int(lines[1].strip())
            except ValueError:
                status = 0
    return body, status


def _bili_curl_args(url: str, sessdata: str = "", cookie_extra: str = "", sep: bool = False) -> list[str]:
    """构建 B站 API curl 命令行参数。"""
    cmd = ["curl", "-s"]
    if sep:
        cmd += ["-w", "\n%{http_code}"]
    parts = []
    if sessdata:
        parts.append(f"SESSDATA={sessdata}")
    if cookie_extra:
        parts.append(cookie_extra)
    if parts:
        cmd += ["-b", "; ".join(parts)]
    cmd.append(url)
    return cmd


# WBI mixin key lookup table (B站混淆表)
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}


class BiliClient:
    """B站 API 客户端，自带WBI签名和反限速。"""

    def __init__(self, sessdata: str = ""):
        self._sessdata = sessdata
        self._client = httpx.AsyncClient(
            headers={**_HEADERS},
            timeout=30,
            follow_redirects=True,
        )
        self._img_key: str = ""
        self._sub_key: str = ""
        self._wbi_ts: float = 0  # last refresh timestamp
        self._buvid3: str = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self._client.aclose()

    # ------------------------------------------------------------------
    # WBI 签名
    # ------------------------------------------------------------------

    async def _refresh_wbi_keys(self):
        """从B站nav接口获取最新的img_key和sub_key。使用 curl 获取。"""
        now = time.time()
        if self._img_key and (now - self._wbi_ts) < 600:  # cache 10min
            return

        url = "https://api.bilibili.com/x/web-interface/nav"
        try:
            body, _ = await _run_curl(_bili_curl_args(url, self._sessdata), timeout=10)
            data = _json.loads(body)
        except Exception as e:
            print(f"[bili] WBI keys curl error: {e}")
            return
        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

        # Extract keys from URLs: .../xxx{key}.jpg
        self._img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
        self._sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""
        self._wbi_ts = now

    def _get_mixin_key(self, raw: str) -> str:
        """Apply the mixin key enc table to produce the 32-char signing key."""
        return "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]

    async def sign_params(self, params: dict) -> dict:
        """Add wbi signature (wts + w_rid) to params dict."""
        await self._refresh_wbi_keys()

        wbi_key = self._get_mixin_key(self._img_key + self._sub_key)
        params["wts"] = int(time.time())

        # Sort params, filter unsafe chars, encode
        filtered = {
            k: "".join(c for c in str(v) if c not in "!'()*")
            for k, v in sorted(params.items())
        }
        query = urllib.parse.urlencode(filtered)
        w_rid = hashlib.md5((query + wbi_key).encode()).hexdigest()

        params["w_rid"] = w_rid
        return params

    # ------------------------------------------------------------------
    # 通用请求（带退避）
    # ------------------------------------------------------------------

    async def _get(self, url: str, params: dict = None, signed: bool = True) -> dict:
        """GET with optional WBI signing and exponential backoff on -412/-352.

        使用 subprocess curl 替代 httpx，绕过容器内 httpx 的 TLS 指纹被 B站 412 拦截。
        """
        if signed and params is not None:
            params = await self.sign_params(params)

        # 构建完整 URL
        if params:
            separator = "&" if "?" in url else "?"
            url = url + separator + urllib.parse.urlencode(params)

        cfg = get_config().get("limits", {})
        backoff_base = cfg.get("backoff_base_seconds", 10)

        for attempt in range(4):
            try:
                body, status_code = await _run_curl(
                    _bili_curl_args(url, self._sessdata, sep=True), timeout=20,
                )
                text = body.decode("utf-8", errors="replace")
            except TimeoutError as e:
                print(f"[bili] curl error: {e}")
                if attempt < 3:
                    wait = backoff_base * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                return {"code": -1, "message": str(e)}
            except Exception as e:
                print(f"[bili] curl error: {e}")
                if attempt < 3:
                    wait = backoff_base * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                return {"code": -1, "message": f"curl error: {e}"}

            # status_code 已由 _run_curl 分离
            if status_code and status_code != 200:
                print(f"[bili] HTTP {status_code}")
                if attempt < 3:
                    wait = backoff_base * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                return {"code": -1, "message": f"HTTP {status_code}"}

            if not text.strip():
                if attempt < 3:
                    wait = backoff_base * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                return {"code": -1, "message": "curl: empty response"}

            try:
                result = _json.loads(text)
            except _json.JSONDecodeError:
                print(f"[bili] non-JSON response ({status_code})")
                if attempt < 3:
                    wait = backoff_base * (2 ** attempt)
                    print(f"[bili] backoff {wait}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                    continue
                return {"code": -1, "message": f"non-JSON response {status_code}"}

            code = result.get("code", 0)

            if code == 0:
                return result

            # Rate limited
            if code in (-412, -352):
                wait = backoff_base * (2 ** attempt)
                print(f"[bili] rate limited (code={code}), backoff {wait}s (attempt {attempt+1})")
                await asyncio.sleep(wait)
                continue

            # Other errors — raise instead of silently returning
            print(f"[bili] API error: code={code} msg={result.get('message', '')}")
            raise BiliAPIError(code, result.get("message", ""), result)

        return {"code": -1, "message": "max retries exceeded"}

    # ------------------------------------------------------------------
    # 视频列表
    # ------------------------------------------------------------------

    async def get_video_list(self, mid: int, page: int = 1, page_size: int = 30) -> dict:
        """获取用户视频列表 (WBI signed)。

        Returns: {"list": {"vlist": [...]}, "page": {...}}
        """
        params = {
            "mid": mid,
            "ps": page_size,
            "pn": page,
            "order": "pubdate",
            "keyword": "",
            "tid": 0,
        }
        result = await self._get(
            "https://api.bilibili.com/x/space/wbi/arc/search",
            params=params,
            signed=True,
        )
        return result.get("data", {})

    # ------------------------------------------------------------------
    # 动态列表
    # ------------------------------------------------------------------

    async def get_dynamics(self, mid: int, offset: str = "") -> dict:
        """获取用户动态列表。使用桌面端endpoint + buvid3防412。使用 curl 获取。

        Returns: {"items": [...], "has_more": bool, "offset": str}
        """
        # Ensure buvid3 cookie
        await self._ensure_buvid3()

        params = {
            "host_mid": mid,
            "offset": offset,
            "timezone_offset": "-480",
        }
        query = urllib.parse.urlencode(params)
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space?{query}"

        cmd = _bili_curl_args(
            url, self._sessdata,
            cookie_extra=f"buvid3={self._buvid3}" if self._buvid3 else "",
            sep=True,
        )
        try:
            body, status_code = await _run_curl(cmd, timeout=15)
            if status_code != 200:
                print(f"[bili] dynamics {status_code}")
                return {}
            result = _json.loads(body)
        except (TimeoutError, Exception) as e:
            print(f"[bili] dynamics curl error: {e}")
            return {}
        return result.get("data", {})

    async def _ensure_buvid3(self):
        """获取buvid3设备标识cookie。使用 curl 获取。"""
        if self._buvid3:
            return
        try:
            body, _ = await _run_curl(
                _bili_curl_args("https://api.bilibili.com/x/frontend/finger/spi"),
                timeout=10,
            )
            data = _json.loads(body).get("data", {})
            self._buvid3 = data.get("b_3", "")
        except Exception:
            self._buvid3 = ""

    # ------------------------------------------------------------------
    # 字幕获取
    # ------------------------------------------------------------------

    async def get_subtitle_url(self, bvid: str, cid: int) -> Optional[str]:
        """获取AI中文字幕URL（需SESSDATA + WBI签名）。使用 curl 获取。

        Returns subtitle URL or None.
        """
        if not self._sessdata:
            return None

        # 需要WBI签名
        params = {"bvid": bvid, "cid": cid}
        signed_params = await self.sign_params(params)
        query = "&".join([f"{k}={v}" for k, v in signed_params.items()])
        url = f"https://api.bilibili.com/x/player/wbi/v2?{query}"
        
        try:
            body, _ = await _run_curl(
                _bili_curl_args(url, self._sessdata), timeout=10,
            )
            data = _json.loads(body).get("data", {})
        except Exception as e:
            print(f"[bili] AI字幕 curl error {bvid}: {e}")
            return None
        subtitles = data.get("subtitle", {}).get("subtitles", [])

        # Prefer ai-zh, then any Chinese
        for sub in subtitles:
            if sub.get("lan") == "ai-zh":
                return "https:" + sub["subtitle_url"]
        for sub in subtitles:
            if sub.get("lan", "").startswith("zh"):
                return "https:" + sub["subtitle_url"]
        return None

    async def get_cc_subtitle(self, bvid: str, cid: int) -> Optional[str]:
        """获取CC字幕URL（不需要cookie）。使用 curl 获取。

        Returns subtitle URL or None.
        """
        url = f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
        try:
            body, _ = await _run_curl(_bili_curl_args(url), timeout=10)
            data = _json.loads(body).get("data", {})
        except Exception as e:
            print(f"[bili] CC字幕 curl error {bvid}: {e}")
            return None
        subtitles = data.get("subtitle", {}).get("subtitles", [])

        for sub in subtitles:
            if sub.get("lan", "").startswith("zh"):
                return "https:" + sub["subtitle_url"]
        return None

    # ------------------------------------------------------------------
    # 视频详情
    # ------------------------------------------------------------------

    async def get_video_info(self, bvid: str) -> dict:
        """获取视频详情（cid、时长、标题等）。"""
        params = {"bvid": bvid}
        result = await self._get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params,
            signed=False,
        )
        return result.get("data", {})

    # ------------------------------------------------------------------
    # 音频下载（ASR用）
    # ------------------------------------------------------------------

    async def get_audio_url(self, bvid: str, cid: int) -> Optional[str]:
        """获取音频流URL。"""
        params = {"bvid": bvid, "cid": cid, "fnval": 16}
        result = await self._get(
            "https://api.bilibili.com/x/player/playurl",
            params=params,
            signed=False,
        )
        dash = result.get("data", {}).get("dash", {})
        audio_list = dash.get("audio", [])
        if audio_list:
            return audio_list[0].get("baseUrl") or audio_list[0].get("base_url")
        return None

    async def download_audio(self, url: str) -> bytes:
        """下载音频二进制。"""
        headers = {**_HEADERS}
        resp = await self._client.get(url, headers=headers)
        return resp.content

    # ------------------------------------------------------------------
    # 评论
    # ------------------------------------------------------------------

    async def get_comments(self, oid: int, oid_type: int = 1, count: int = 20) -> dict:
        """获取评论列表。

        oid_type: 1=视频, 17=动态, 11=图文
        Returns {"replies": list, "total": int}.
        """
        params = {
            "oid": oid,
            "type": oid_type,
            "sort": 2,  # 按热度
            "pn": 1,
            "ps": min(count, 20),
        }
        result = await self._get(
            "https://api.bilibili.com/x/v2/reply",
            params=params,
            signed=False,
        )
        data = result.get("data", {})
        replies = data.get("replies", []) or []
        total = data.get("page", {}).get("count", len(replies))
        return {"replies": replies[:count], "total": total}

    # ------------------------------------------------------------------
    # 弹幕
    # ------------------------------------------------------------------

    async def get_danmaku(self, cid: int, max_count: int = 2000) -> list:
        """获取弹幕列表（protobuf接口，返回文本列表）。使用 curl 获取。"""
        import re
        url = f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"
        try:
            body, _ = await _run_curl(_bili_curl_args(url), timeout=15)
            text = body.decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"[bili] danmaku curl error: {e}")
            return []
        danmakus = re.findall(r'<d [^>]*>(.*?)</d>', text)
        if len(danmakus) > max_count:
            import random
            danmakus = random.sample(danmakus, max_count)
        return danmakus

    # ------------------------------------------------------------------
    # Cookie验证
    # ------------------------------------------------------------------

    async def validate_sessdata(self) -> dict:
        """验证SESSDATA是否有效。使用 curl 获取。

        Returns: {"valid": bool, "username": str, "expire_date": str}
        """
        if not self._sessdata:
            return {"valid": False, "username": "", "expire_date": ""}

        try:
            body, _ = await _run_curl(
                _bili_curl_args(
                    "https://api.bilibili.com/x/web-interface/nav",
                    self._sessdata,
                ),
                timeout=10,
            )
            data = _json.loads(body).get("data", {})
        except Exception as e:
            print(f"[bili] validate_sessdata curl error: {e}")
            return {"valid": False, "username": "", "expire_date": ""}
        is_login = data.get("isLogin", False)
        uname = data.get("uname", "")

        # Parse expire from SESSDATA timestamp
        # SESSDATA format: xxx,timestamp,xxx
        expire_date = ""
        try:
            parts = self._sessdata.split(",")
            if len(parts) >= 2:
                ts = int(parts[1])
                expire_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            pass

        return {"valid": is_login, "username": uname, "expire_date": expire_date}

    # ------------------------------------------------------------------
    # 用户搜索
    # ------------------------------------------------------------------

    async def search_user(self, keyword: str) -> Optional[dict]:
        """通过用户名搜索B站用户，返回第一个匹配结果。

        使用 wbi/search/all/v2 接口（比 search/type 更稳定，不易被412）。
        Returns: {"mid": int, "name": "", "sign": ""} 或 None
        """
        params = {
            "keyword": keyword,
        }
        result = await self._get(
            "https://api.bilibili.com/x/web-interface/wbi/search/all/v2",
            params=params,
            signed=True,
        )

        if result.get("code") != 0:
            print(f"[bili] search_user failed: {result}")
            return None

        # 从 result 列表中找 bili_user 类型的结果
        for r in result.get("data", {}).get("result", []):
            if r.get("result_type") == "bili_user":
                users = r.get("data", [])
                if users:
                    user = users[0]
                    return {
                        "mid": user.get("mid"),
                        "name": user.get("uname", ""),
                        "sign": user.get("usign", ""),
                    }
        return None

    async def get_followings(self, vmid: int, pn: int = 1, ps: int = 50) -> dict:
        """获取用户所有关注（需要SESSDATA）。自动翻页获取全部。

        Returns: {"total": int, "list": [{"mid": int, "name": str, "sign": str}, ...]}
        """
        all_followings = []
        total = 0
        page = pn

        while True:
            url = f"https://api.bilibili.com/x/relation/followings?vmid={vmid}&pn={page}&ps={ps}&order=desc"
            try:
                body, _ = await _run_curl(
                    _bili_curl_args(url, self._sessdata), timeout=10,
                )
                data = _json.loads(body)
            except Exception as e:
                print(f"[bili] get_followings curl error: {e}")
                return {"total": total, "list": all_followings}

            if data.get("code") != 0:
                print(f"[bili] get_followings failed: code={data.get('code')}, msg={data.get('message')}")
                return {"total": total, "list": all_followings}

            page_data = data.get("data", {})
            total = page_data.get("total", 0)
            page_list = page_data.get("list", [])

            for f in page_list:
                all_followings.append({
                    "mid": f.get("mid"),
                    "name": f.get("uname", ""),
                    "sign": f.get("sign", ""),
                })

            # B站关注接口上限500页（ps=50时最多25000条），防止无限循环
            if len(page_list) < ps or page >= 500:
                break
            page += 1
            await asyncio.sleep(0.5)

        return {"total": total, "list": all_followings}
