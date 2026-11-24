"""Local-network listening survey. Run directly or through the project entry point."""

import argparse
import json
import logging
import os
import random
import re
import socket
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from urllib.parse import urlsplit


PACKAGE = Path(__file__).resolve().parent
STATIC = PACKAGE / "static"
AUDIO_TYPES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm",
}
DIMENSIONS = {
    "timbre": ("音色接近真人", "从开口到句尾，这段声音听起来有多像真人的嗓音？"),
    "intonation": ("语调和重音", "声音的起伏和读重的地方，是否符合这句话的意思？"),
    "pacing": ("语速和停顿", "说话的快慢、停顿的位置和长短，听起来自然吗？"),
    "expression": ("语气和表现力", "说话人的语气和情绪有多贴合这段话的内容？"),
    "clarity": ("咬字和内容清晰度", "只听录音，你能轻松听清每个词、听懂整句话吗？"),
}
TOKEN = r"[0-9a-f]{32}"
ROUND_TTL = 24 * 60 * 60


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SurveyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class Round:
    id: str
    ip: str
    started_at: str
    expires: float
    reference: Path | None
    questions: list[list[str]]
    files: dict[str, Path]

    def public(self):
        prefix = f"/audio/{self.id}/"
        return {
            "id": self.id,
            "reference_url": prefix + "reference" if self.reference else None,
            "questions": [
                [{"id": key, "audio_url": prefix + key} for key in question]
                for question in self.questions
            ],
        }


class Survey:
    def __init__(self, root, questions=None, samples=2):
        self.datasets = root / "datasets"
        self.results = root / "result"
        self.questions = questions
        self.samples = samples
        self.pending = {}
        self.lock = threading.RLock()
        for folder in (self.datasets / "raw", self.datasets / "test", self.results):
            folder.mkdir(parents=True, exist_ok=True)

    def inventory(self):
        return {
            group: sorted(
                p for p in (self.datasets / group).iterdir()
                if p.suffix.lower() in AUDIO_TYPES and p.is_file() and not p.is_symlink()
            )
            for group in ("raw", "test")
        }

    def question_count(self, available):
        if self.questions is not None:
            return self.questions
        if 0 < available <= self.samples:
            return 1
        return available

    def setup(self):
        pools = self.inventory()
        return {
            "questions": self.question_count(len(pools["test"])),
            "samples": min(self.samples, len(pools["test"])),
            "has_reference": bool(pools["raw"]),
            "dimensions": [
                {"id": key, "label": label, "hint": hint}
                for key, (label, hint) in DIMENSIONS.items()
            ],
        }

    def draw_groups(self, paths):
        count = min(self.samples, len(paths))
        total = self.question_count(len(paths))
        if self.questions is None:
            # Take consecutive groups around a shuffled circle, including the wraparound.
            shuffled = random.sample(paths, len(paths))
            circle = shuffled + shuffled[:count - 1]
            return [circle[i:i + count] for i in range(total)]

        uses = dict.fromkeys(paths, 0)
        groups = []
        for _ in range(total):
            group = sorted(paths, key=lambda p: (uses[p], random.random()))[:count]
            for path in group:
                uses[path] += 1
            groups.append(group)
        return groups

    def start(self, ip):
        pools = self.inventory()
        paths = pools["test"]
        if not paths:
            raise SurveyError(409, "暂无待评录音，请先向 datasets/test 放入音频。")
        reference = random.choice(pools["raw"]) if pools["raw"] else None
        files, questions = {}, []
        for group in self.draw_groups(paths):
            ids = []
            for path in group:
                key = uuid.uuid4().hex
                files[key] = path
                ids.append(key)
            random.shuffle(ids)
            questions.append(ids)
        random.shuffle(questions)
        now = time.monotonic()
        session = Round(uuid.uuid4().hex, ip, timestamp(), now + ROUND_TTL,
                        reference, questions, files)
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if v.expires > now}
            self.pending[session.id] = session
        return session.public()

    def active(self, key, ip):
        with self.lock:
            session = self.pending.get(key)
            if not session or session.ip != ip or session.expires <= time.monotonic():
                raise SurveyError(404, "本轮答题已失效，请重新开始。")
            return session

    def receipt(self, key, ip):
        path = self.results / f"{key}.json"
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["participant_ip"] == ip:
                return {"id": key, "submitted_at": saved["submitted_at"]}
        return None

    def status(self, key, ip):
        with self.lock:
            return self.receipt(key, ip) or self.active(key, ip).public()

    def submit(self, key, ip, ratings):
        with self.lock:
            # Network retries of an accepted submission return the original receipt.
            if receipt := self.receipt(key, ip):
                return receipt
            session = self.active(key, ip)
            if not isinstance(ratings, dict) or ratings.keys() != session.files.keys():
                raise SurveyError(400, "请完成本轮所有样本的评分。")
            questions = []
            for number, ids in enumerate(session.questions, 1):
                samples = []
                for sample_id in ids:
                    answer = ratings[sample_id]
                    if not isinstance(answer, dict):
                        raise SurveyError(400, "每个样本的评分格式无效。")
                    scores = answer.copy()
                    feedback = scores.pop("feedback", "")
                    if not isinstance(feedback, str) or re.search(r"[\ud800-\udfff]", feedback):
                        raise SurveyError(400, "样本反馈需要是有效的文本。")
                    if (scores.keys() != DIMENSIONS.keys()
                            or any(type(v) is not int or not 1 <= v <= 5 for v in scores.values())):
                        raise SurveyError(400, "每个样本的五个维度都需要选择 1 至 5 的整数分。")
                    path = session.files[sample_id]
                    samples.append({
                        "sample_id": sample_id,
                        "source": "clone",
                        "file": path.relative_to(self.datasets).as_posix(),
                        "scores": scores,
                        "feedback": feedback,
                        "authenticity_mean": round(sum(scores.values()) / len(scores), 2),
                    })
                questions.append({"number": number, "samples": samples})
            submitted = timestamp()
            result = {
                "attempt_id": key, "participant_ip": ip,
                "started_at": session.started_at, "submitted_at": submitted,
                "reference_file": (session.reference.relative_to(self.datasets).as_posix()
                                   if session.reference else None),
                "scale": {"min": 1, "max": 5},
                "dimensions": {k: v[0] for k, v in DIMENSIONS.items()},
                "questions": questions,
            }
            # Publish the complete JSON in one rename on the same filesystem.
            out = tempfile.NamedTemporaryFile(dir=self.results, suffix=".tmp", delete=False,
                                              mode="w", encoding="utf-8")
            temp_path = Path(out.name)
            try:
                with out:
                    json.dump(result, out, ensure_ascii=False, indent=2)
                    out.write("\n")
                os.replace(temp_path, self.results / f"{key}.json")
            finally:
                temp_path.unlink(missing_ok=True)
            del self.pending[key]
            return {"id": key, "submitted_at": submitted}


def byte_range(header, size):
    """Return inclusive bounds of a single HTTP byte range, including suffix ranges."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if match and size:
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        elif last:
            start, end = max(0, size - int(last)), size - 1
        else:
            return None
        if start <= end:
            return start, end
    return None


class Handler(BaseHTTPRequestHandler):
    @property
    def survey(self):
        return self.server.survey

    def reply(self, status, body=b"", mime="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def audio(self, session, sample):
        path = session.reference if sample == "reference" else session.files.get(sample)
        if not path:
            raise SurveyError(404, "找不到此样本。")
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            raise SurveyError(404, "音频文件已移除。") from None
        with stream:
            size = os.fstat(stream.fileno()).st_size
            start, end = 0, size - 1
            partial = self.headers.get("Range")
            if partial is not None:
                bounds = byte_range(partial, size) if len(partial) < 128 else None
                if bounds is None:
                    self.reply(416, headers={"Content-Range": f"bytes */{size}"})
                    return
                start, end = bounds
            self.send_response(206 if partial else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", AUDIO_TYPES[path.suffix.lower()])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, no-store")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0 and (chunk := stream.read(min(65536, remaining))):
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def dispatch(self):
        route = urlsplit(self.path).path
        ip = self.client_address[0]
        if self.command == "POST":
            if self.headers.get_content_type() != "application/json":
                raise SurveyError(415, "需要 JSON 格式。")
            length = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,8}", length) or int(length) > 2_000_000:
                raise SurveyError(413, "提交内容过大或长度无效。")
            try:
                body = json.loads(self.rfile.read(int(length)))
            except (ValueError, RecursionError):
                raise SurveyError(400, "JSON 格式无效。") from None
            if not isinstance(body, dict):
                raise SurveyError(400, "需要 JSON 对象。")
            if route == "/api/rounds":
                return self.survey.start(ip)
            if match := re.fullmatch(rf"/api/rounds/({TOKEN})/submit", route):
                return self.survey.submit(match[1], ip, body.get("ratings"))
        else:
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/style.css": ("style.css", "text/css")}
            if route in assets:
                name, mime = assets[route]
                self.reply(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
                return
            if route == "/api/setup":
                return self.survey.setup()
            if match := re.fullmatch(rf"/api/rounds/({TOKEN})", route):
                return self.survey.status(match[1], ip)
            if match := re.fullmatch(rf"/audio/({TOKEN})/(reference|{TOKEN})", route):
                self.audio(self.survey.active(match[1], ip), match[2])
                return
        raise SurveyError(404, "找不到此页面。")

    def handle_request(self):
        try:
            try:
                response = self.dispatch()
                status = HTTPStatus.OK
            except SurveyError as error:
                status, response = error.status, {"error": str(error)}
            except (ConnectionError, TimeoutError):
                return
            except OSError:
                logging.exception("Survey file access failed")
                status, response = 500, {"error": "文件读写失败，请检查服务端目录后重试。"}
            if response is not None:
                self.reply(status, json.dumps(response, ensure_ascii=False).encode())
        except (ConnectionError, TimeoutError):
            pass  # Browsers cancel audio requests when switching samples.

    do_GET = do_HEAD = do_POST = handle_request


class SurveyServer(ThreadingHTTPServer):
    def __init__(self, address, survey):
        self.survey = survey
        super().__init__(address, Handler)


def lan_ipv4():
    addresses = []
    try:
        # Ask the OS for the LAN multicast route without sending a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("239.255.255.250", 1900))
            addresses.append(route.getsockname()[0])
    except OSError:
        pass
    try:
        addresses.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass

    private_lans = (IPv4Network("10.0.0.0/8"), IPv4Network("172.16.0.0/12"),
                    IPv4Network("192.168.0.0/16"))
    fallback = None
    for address in addresses:
        ip = IPv4Address(address)
        if any(ip in network for network in private_lans):
            return address
        if fallback is None and (ip.is_global or ip.is_link_local) and not ip.is_multicast:
            fallback = address
    return fallback or "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="局域网语音真实性盲评")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true", help="启动后在默认浏览器打开答题首页")
    parser.add_argument("--questions", type=int, help="指定每轮题数（1–100），默认按待评录音数量出题")
    parser.add_argument("--samples-per-question", type=int, default=2,
                        help="每题样本数（1–20，最多为 datasets/test 中的音频数量）")
    args = parser.parse_args()
    if ((args.questions is not None and not 1 <= args.questions <= 100)
            or not 1 <= args.samples_per_question <= 20):
        parser.error("每轮题数应为 1–100，每题样本数应为 1–20。")
    survey = Survey(PACKAGE, args.questions, args.samples_per_question)
    with SurveyServer((args.host, args.port), survey) as server:
        host, port = server.server_address
        url = f"http://{lan_ipv4() if host == '0.0.0.0' else host}:{port}/"
        print(f"Listening on {args.host}:{port} | Results: {survey.results}", flush=True)
        print(f"答题首页 {url}  请保持此窗口开启。", flush=True)
        if args.open_browser:
            threading.Thread(target=webbrowser.open_new_tab, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
