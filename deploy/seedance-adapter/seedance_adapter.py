#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ark Seedance <-> OpenAI Video protocol adapter for Sub2API.

Sub2API 的 Seedance 通道按火山方舟(Ark)异步任务协议发请求:
    POST   {base}/api/v3/contents/generations/tasks
    GET    {base}/api/v3/contents/generations/tasks/{task_id}
    DELETE {base}/api/v3/contents/generations/tasks/{task_id}

上游只实现 OpenAI 视频协议:
    POST   /v1/videos          {"model","prompt","seconds","size","image"}
    GET    /v1/videos/{id}

参考素材能力（已实测）:
    - 只支持图片，且必须是公网可达的 https URL
    - 不支持 base64 / data URL，不支持 http://
    - 不支持音频、视频参考素材（上游无对应能力）
    不被支持的素材会被显式拒绝，绝不静默丢弃，避免生成结果与预期不符。

上游 API Key 复用 Sub2API 转发时携带的 Authorization 头, 本服务不保存任何密钥。
仅使用 Python 标准库。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "").rstrip("/")
PORT = int(os.environ.get("PORT", "9000"))
CREATE_PATH = os.environ.get("UPSTREAM_CREATE_PATH", "/v1/videos")
STATUS_PATH = os.environ.get("UPSTREAM_STATUS_PATH", "/v1/videos/{id}")
TOKENS_PER_SECOND_720P = float(os.environ.get("TOKENS_PER_SECOND_720P", "5000"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "120"))

ARK_CREATE = "/api/v3/contents/generations/tasks"
ARK_TASK_RE = re.compile(r"^/api/v3/contents/generations/tasks/([^/?#]+)")

# 上游只吃得下文字和单张参考图
ALLOWED_CONTENT_TYPES = ("text", "image_url")
MAX_REFERENCE_IMAGES = 1

LONG_SIDE = {
    "480p": 854, "540p": 960, "720p": 1280, "1080p": 1920,
    "1440p": 2560, "2k": 2560, "4k": 3840,
}
RATIOS = {
    "16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:3": (4, 3),
    "3:4": (3, 4), "21:9": (21, 9), "9:21": (9, 21), "3:2": (3, 2), "2:3": (2, 3),
}
STATUS_MAP = {
    "queued": "queued", "pending": "queued", "created": "queued",
    "running": "running", "processing": "running", "in_progress": "running",
    "succeeded": "succeeded", "success": "succeeded",
    "completed": "succeeded", "done": "succeeded",
    "failed": "failed", "error": "failed",
    "cancelled": "cancelled", "canceled": "cancelled",
}


def normalize_size(resolution, ratio):
    """720p + 16:9 -> 1280x720"""
    long_side = LONG_SIDE.get((resolution or "").strip().lower())
    pair = RATIOS.get((ratio or "").strip())
    if not long_side or not pair:
        return None
    a, b = pair
    if a >= b:
        w, h = long_side, int(round(long_side * b / a))
    else:
        h, w = long_side, int(round(long_side * a / b))
    return "%dx%d" % (w - w % 2, h - h % 2)


def ark_to_openai(body):
    """Ark content[] 请求体 -> OpenAI 视频请求体。

    返回 (converted, error_message)。音频/视频等不支持的素材在这里显式报错，
    不再静默丢弃。
    """
    texts, images = [], []
    for idx, item in enumerate(body.get("content") or []):
        if not isinstance(item, dict):
            return None, "content[%d] must be an object" % idx
        kind = str(item.get("type") or "").strip()
        if kind not in ALLOWED_CONTENT_TYPES:
            return None, (
                "content type '%s' is not supported; only 'text' and 'image_url' "
                "are accepted (audio and video references are unavailable)" % kind
            )
        if kind == "text":
            texts.append(item.get("text") or "")
            continue
        url = str((item.get("image_url") or {}).get("url") or "").strip()
        if not url:
            return None, "content[%d].image_url.url is required" % idx
        if not url.startswith("https://"):
            return None, (
                "content[%d].image_url.url must be a public https URL; "
                "base64/data URLs and http:// are not supported" % idx
            )
        images.append(url)

    if len(images) > MAX_REFERENCE_IMAGES:
        return None, (
            "only %d reference image is supported, got %d"
            % (MAX_REFERENCE_IMAGES, len(images))
        )

    out = {"model": body.get("model")}
    prompt = "\n".join(x for x in texts if x).strip()
    if prompt:
        out["prompt"] = prompt
    if images:
        out["image"] = images[0]
    if body.get("duration") is not None:
        out["seconds"] = str(body["duration"])
    size = normalize_size(body.get("resolution"), body.get("ratio"))
    if size:
        out["size"] = size
    return out, None


def dig(obj, path):
    cur = obj
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or len(cur) <= key:
                return None
            cur = cur[key]
        else:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
    return cur


def find_video_url(payload):
    for path in (("data", 0, "url"), ("video_url",), ("url",), ("output", "url"),
                 ("result", "url"), ("metadata", "url"), ("video", "url"),
                 ("content", "video_url"), ("content", "url"), ("data", "url")):
        value = dig(payload, path)
        if isinstance(value, str) and value.startswith("http"):
            return value
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and value.startswith("http") and (
                        ".mp4" in value or "video" in key.lower() or "/videos/" in value):
                    found.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found[0] if found else None


def estimate_completion_tokens(payload):
    """Sub2API 在 status==succeeded 时按 usage.completion_tokens 计费。
    上游不回 token 数, 这里按 720p 每秒基准 + 像素比例折算,
    需与分组里该模型的 output token 单价对齐才能反映真实成本。"""
    for path in (("usage", "completion_tokens"), ("usage", "total_tokens"),
                 ("usage", "output_tokens")):
        value = dig(payload, path)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    raw = str(dig(payload, ("size",)) or dig(payload, ("metadata", "size")) or "")
    match = re.match(r"^(\d+)\s*[x*]\s*(\d+)$", raw)
    w, h = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    try:
        seconds = float(dig(payload, ("seconds",)) or dig(payload, ("duration",)) or 5)
    except (TypeError, ValueError):
        seconds = 5.0
    scale = (w * h) / (1280 * 720) if w and h else 1.0
    return max(1, int(round(TOKENS_PER_SECOND_720P * seconds * scale)))


def to_ark_status(payload, task_id):
    status = STATUS_MAP.get(str(payload.get("status", "")).strip().lower(), "running")
    out = {"id": task_id, "model": payload.get("model"), "status": status}
    if payload.get("progress") is not None:
        out["progress"] = payload.get("progress")
    if status == "succeeded":
        url = find_video_url(payload)
        if url:
            out["content"] = {"video_url": url}
        out["usage"] = {"completion_tokens": estimate_completion_tokens(payload)}
    elif status == "failed":
        err = payload.get("error") or {}
        out["error"] = {
            "code": err.get("code") or "generation_failed",
            "message": err.get("message") or "upstream video generation failed",
        }
    return out


class AdapterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "seedance-adapter/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[adapter] " + (fmt % args) + "\n")
        sys.stderr.flush()

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, code, message, err_type="invalid_request_error"):
        self._send_json(code, {"error": {"message": message, "type": err_type}})

    def _upstream(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(UPSTREAM_BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        auth = self.headers.get("Authorization")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:  # noqa: BLE001
            return 502, json.dumps(
                {"error": {"message": str(exc), "type": "upstream_unreachable"}}
            ).encode("utf-8")

    def _forward_create(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return self._fail(400, "invalid JSON body")
        if not isinstance(body, dict):
            return self._fail(400, "request body must be a JSON object")

        converted, err_msg = ark_to_openai(body)
        if err_msg:
            self.log_message("rejected content: %s", err_msg)
            return self._fail(400, err_msg)

        if not converted.get("model"):
            return self._fail(400, "model is required")
        if not converted.get("prompt"):
            return self._fail(400, "content must contain at least one text item")

        code, raw = self._upstream("POST", CREATE_PATH, converted)
        try:
            upstream = json.loads(raw)
        except json.JSONDecodeError:
            return self._fail(code, raw[:400].decode("utf-8", "replace"), "upstream_error")
        if code >= 300:
            return self._send_json(code, upstream)
        task_id = upstream.get("id") or upstream.get("task_id") or upstream.get("request_id")
        if not task_id:
            self.log_message("create response missing task id: %s", raw[:300])
            return self._send_json(502, {"error": {
                "message": "upstream create response missing task id", "type": "upstream_error"}})
        self.log_message("created task=%s model=%s image=%s", task_id,
                         converted.get("model"), "yes" if converted.get("image") else "no")
        return self._send_json(200, {
            "id": task_id, "model": converted.get("model"), "status": "queued"})

    def _forward_status(self, task_id):
        path = STATUS_PATH.replace("{id}", urllib.parse.quote(task_id, safe=""))
        code, raw = self._upstream("GET", path)
        try:
            upstream = json.loads(raw)
        except json.JSONDecodeError:
            return self._fail(code, raw[:400].decode("utf-8", "replace"), "upstream_error")
        if code >= 300 and code != 404:
            return self._send_json(code, upstream)
        return self._send_json(200, to_ark_status(upstream, task_id))

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != ARK_CREATE:
            return self._fail(404, "not found")
        self._forward_create()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        match = ARK_TASK_RE.match(path)
        if not match:
            return self._fail(404, "not found")
        self._forward_status(match.group(1))

    def do_DELETE(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        match = ARK_TASK_RE.match(path)
        if not match:
            return self._fail(404, "not found")
        self.log_message("delete task=%s (upstream has no delete endpoint)", match.group(1))
        return self._send_json(200, {"id": match.group(1), "deleted": True})


def main():
    if not UPSTREAM_BASE:
        sys.exit("UPSTREAM_BASE is required")
    sys.stderr.write("[adapter] listening on 0.0.0.0:%d -> %s\n" % (PORT, UPSTREAM_BASE))
    sys.stderr.flush()
    ThreadingHTTPServer(("0.0.0.0", PORT), AdapterHandler).serve_forever()


if __name__ == "__main__":
    main()
