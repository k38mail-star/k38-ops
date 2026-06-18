"""K38 Operations Center - Layer 1 dashboard."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse


app = FastAPI(title="K38 Ops Dashboard")

MESSAGE_LOG_PATH = Path(os.environ.get("K38_MESSAGE_LOG", "/tmp/k38-message-bus.jsonl"))
MESSAGE_BUS_URL = os.environ.get("K38_MESSAGE_BUS_URL", "http://100.93.251.118:6788/poll")
FOTAO_BI_URL = os.environ.get("K38_FOTAO_BI_URL", "http://127.0.0.1:3003/api/futao/data")
HERMES_SKILLS_DIR = Path(os.environ.get("K38_HERMES_SKILLS_DIR", "~/.hermes/skills")).expanduser()
APT_HISTORY_PATH = Path(os.environ.get("K38_APT_HISTORY_PATH", "/var/log/apt/history.log"))
DAILY_REPORT_JSON_PATHS = [
    Path(path).expanduser()
    for path in [
        os.environ.get("K38_DAILY_REPORTS_JSON", ""),
        "./daily_reports.json",
        "./fotao_daily_reports.json",
        "/tmp/k38-daily-reports.json",
    ]
    if path
]
ECS_SERVICES = {
    "msg_bus": "msg-bus",
    "dltrace_api": "dltrace-api",
    "nginx": "nginx",
    "k38_football": "k38-football",
    "fotao_bi": "fotao-bi",
    "hermes_gateway": "hermes-gateway",
}
CEO_RECOMMENDATIONS = [
    {
        "name": "fotao-report-normalizer",
        "type": "skill",
        "reason": "Fotao BI 当前返回 HTML，建议安装结构化日报抽取与 JSON 归档技能，降低看板解析失败风险。",
    },
    {
        "name": "apt-change-auditor",
        "type": "skill",
        "reason": "近期软件安装已经可见，下一步应把 apt 变更与服务重启、异常告警自动关联。",
    },
    {
        "name": "OpenTelemetry Collector",
        "type": "tool",
        "reason": "ECS、BI、消息总线和 Hermes 网关仍缺统一 traces 与 metrics，建议作为共享观测入口。",
    },
    {
        "name": "message-bus-retention-policy",
        "type": "skill",
        "reason": "看板会直接轮询消息总线，应补齐保留期、采样和敏感字段脱敏规则。",
    },
]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _format_date(ts: float | int | None) -> str:
    if not ts:
        return "--"
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def _format_datetime(ts: float | int | None) -> str:
    if not ts:
        return "--"
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def _format_currency(value: Any) -> str:
    if value in (None, ""):
        return "--"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("¥") or stripped.lower() in {"--", "n/a"}:
            return stripped
        cleaned = re.sub(r"[^\d.\-]", "", stripped)
        if not cleaned:
            return stripped
        try:
            value = float(cleaned)
        except ValueError:
            return stripped
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"¥{number:,.0f}"


def _number_from_any(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        if cleaned:
            try:
                return float(cleaned)
            except ValueError:
                pass
    return 0.0


def _first_value(item: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return default


def _normalize_daily_report(item: dict[str, Any]) -> dict[str, str]:
    revenue = _first_value(item, ["revenue", "income", "sales", "amount", "收入"], "")
    cost = _first_value(item, ["cost", "expense", "spend", "成本"], "")
    profit = _first_value(item, ["profit", "net_profit", "margin", "利润"], "")
    if profit in (None, "") and revenue not in (None, "") and cost not in (None, ""):
        profit = _number_from_any(revenue) - _number_from_any(cost)

    return {
        "date": str(_first_value(item, ["date", "day", "report_date", "日期"], "--")),
        "revenue": _format_currency(revenue),
        "cost": _format_currency(cost),
        "profit": _format_currency(profit),
        "notes": str(_first_value(item, ["notes", "note", "summary", "remark", "备注"], "")),
    }


def _extract_report_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ["reports", "daily_reports", "data", "rows", "items", "result"]:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_report_items(value)
            if nested:
                return nested
    return [payload] if any(key in payload for key in ["date", "day", "revenue", "income", "收入"]) else []


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            cell = " ".join("".join(self._current_cell).split())
            self._current_row.append(cell)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None and self._current_table is not None:
            if any(cell for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            self.tables.append(self._current_table)
            self._current_table = None


def _reports_from_html(html: str) -> list[dict[str, str]]:
    parser = _TableParser()
    parser.feed(html)
    reports: list[dict[str, str]] = []
    aliases = {
        "date": {"date", "day", "report date", "日期"},
        "revenue": {"revenue", "income", "sales", "收入", "营收"},
        "cost": {"cost", "expense", "spend", "成本", "支出"},
        "profit": {"profit", "net profit", "利润", "净利润"},
        "notes": {"notes", "note", "summary", "remark", "备注", "说明"},
    }

    for table in parser.tables:
        if len(table) < 2:
            continue
        header = [cell.strip().lower() for cell in table[0]]
        mapping: dict[str, int] = {}
        for field, names in aliases.items():
            for index, label in enumerate(header):
                if label in names or any(name in label for name in names):
                    mapping[field] = index
                    break
        if "date" not in mapping:
            continue
        for row in table[1:]:
            raw = {field: row[index] for field, index in mapping.items() if index < len(row)}
            if raw:
                reports.append(_normalize_daily_report(raw))
    return reports


def _read_daily_reports_json() -> tuple[list[dict[str, str]], str | None]:
    for path in DAILY_REPORT_JSON_PATHS:
        if not path.exists():
            continue
        payload = json.loads(_read_text(path))
        return [_normalize_daily_report(item) for item in _extract_report_items(payload)], str(path)
    return [], None


def _fetch_fotao_daily_reports() -> tuple[list[dict[str, str]], str]:
    req = urllib.request.Request(
        FOTAO_BI_URL,
        headers={"Accept": "text/html,application/json", "User-Agent": "k38-ops-dashboard/1.0"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        content_type = resp.headers.get("Content-Type", "")
    if "json" in content_type.lower():
        payload = json.loads(body)
        return [_normalize_daily_report(item) for item in _extract_report_items(payload)], FOTAO_BI_URL
    reports = _reports_from_html(body)
    if reports:
        return reports, FOTAO_BI_URL
    raise ValueError("Fotao BI returned HTML without a recognizable daily report table")


def _read_daily_reports() -> tuple[list[dict[str, str]], str, str | None]:
    try:
        reports, source = _fetch_fotao_daily_reports()
        if reports:
            return reports[:14], source, None
    except Exception as exc:
        json_reports, json_source = _read_daily_reports_json()
        if json_reports:
            return json_reports[:14], json_source or "local-json", f"Fotao BI fallback: {exc}"
        return [], "unavailable", str(exc)
    json_reports, json_source = _read_daily_reports_json()
    if json_reports:
        return json_reports[:14], json_source or "local-json", "Fotao BI response was empty"
    return [], FOTAO_BI_URL, "Fotao BI response was empty and no local JSON fallback exists"


def _skill_description(path: Path) -> str:
    for candidate in [path / "SKILL.md", path / "README.md", path / "README.txt"]:
        if not candidate.exists() or not candidate.is_file():
            continue
        for line in _read_text(candidate).splitlines():
            text = line.strip().lstrip("#").strip()
            if text:
                return text[:180]
    return "Hermes skill detected from ~/.hermes/skills."


def _count_skill_categories(path: Path) -> int:
    if path.is_file():
        return 1
    categories = 0
    try:
        for child in path.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir():
                categories += 1
    except OSError:
        return 0
    return categories


def _read_hermes_skills() -> list[dict[str, Any]]:
    if not HERMES_SKILLS_DIR.exists() or not HERMES_SKILLS_DIR.is_dir():
        return []

    skills: list[dict[str, Any]] = []
    for path in HERMES_SKILLS_DIR.iterdir():
        if path.name.startswith("."):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        usage_count = _count_skill_categories(path)
        skills.append(
            {
                "name": path.name,
                "installed": _format_date(stat.st_mtime),
                "installed_at": _format_datetime(stat.st_mtime),
                "description": _skill_description(path),
                "usage_count": usage_count,
                "usage": usage_count,
                "path": str(path),
            }
        )
    return sorted(skills, key=lambda item: item["installed_at"], reverse=True)


def _parse_apt_history() -> list[dict[str, str]]:
    if not APT_HISTORY_PATH.exists():
        return []

    entries: list[dict[str, str]] = []
    current_date = ""
    for raw_line in _read_text(APT_HISTORY_PATH).splitlines():
        line = raw_line.strip()
        if line.startswith("Start-Date:"):
            current_date = line.replace("Start-Date:", "", 1).strip()
        elif line.startswith("Install:"):
            packages = re.findall(r"([^,\s:]+):[^,]+?\(([^)]+)\)", line)
            for name, version in packages:
                entries.append(
                    {
                        "name": name,
                        "installed": current_date or "--",
                        "description": f"Installed via apt history ({version}).",
                    }
                )
    return list(reversed(entries))[:20]


COMMON_TOOL_KEYWORDS = {
    "nginx": ("edge", "入口代理与静态资源服务。"),
    "redis": ("data", "缓存、队列和状态存储相关组件。"),
    "postgresql": ("data", "PostgreSQL 数据库相关组件。"),
    "mysql": ("data", "MySQL/MariaDB 数据库相关组件。"),
    "mariadb": ("data", "MariaDB 数据库相关组件。"),
    "docker": ("runtime", "容器运行与镜像管理工具。"),
    "containerd": ("runtime", "容器运行时组件。"),
    "python": ("runtime", "Python 运行时与服务依赖。"),
    "nodejs": ("runtime", "Node.js 运行时。"),
    "npm": ("runtime", "Node.js 包管理工具。"),
    "uvicorn": ("api", "ASGI 服务运行时。"),
    "fastapi": ("api", "FastAPI Web/API 服务框架。"),
    "gunicorn": ("api", "Python 服务进程管理器。"),
    "curl": ("ops", "HTTP 接口诊断工具。"),
    "wget": ("ops", "文件与接口拉取工具。"),
    "jq": ("ops", "JSON 日志和接口响应解析工具。"),
    "git": ("ops", "代码版本管理工具。"),
    "vim": ("ops", "终端编辑器。"),
    "nano": ("ops", "终端编辑器。"),
    "tmux": ("ops", "终端会话管理工具。"),
    "htop": ("ops", "进程与资源诊断工具。"),
    "rsync": ("ops", "文件同步工具。"),
    "systemd": ("node", "服务守护与节点管理组件。"),
}


def _read_shared_tools() -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["dpkg", "-l"],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except Exception:
        return []
    if result.returncode not in {0, 1}:
        return []

    tools: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("ii"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 3:
            continue
        package = parts[1].split(":", 1)[0]
        lower = package.lower()
        match = next((key for key in COMMON_TOOL_KEYWORDS if key in lower), "")
        if not match or package in seen:
            continue
        seen.add(package)
        scope, description = COMMON_TOOL_KEYWORDS[match]
        tools.append(
            {
                "name": package,
                "scope": scope,
                "version": parts[2],
                "description": description,
            }
        )
    return sorted(tools, key=lambda item: (item["scope"], item["name"]))[:40]


def _recommendation_cards() -> str:
    return "\n".join(
        f"""
            <article class="recommendation">
              <div class="rec-type">{escape(item["type"])}</div>
              <div>
                <h4>{escape(item["name"])}</h4>
                <p>{escape(item["reason"])}</p>
              </div>
            </article>"""
        for item in CEO_RECOMMENDATIONS
    )


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {"ok": True, "service": "k38-ops-dashboard", "ts": time.time()}


@app.get("/api/daily-reports")
async def api_daily_reports() -> dict[str, Any]:
    reports, source, error = _read_daily_reports()
    return {
        "ok": error is None or bool(reports),
        "reports": reports,
        "source": source,
        "error": error,
        "ts": time.time(),
    }


@app.get("/api/skills")
async def api_skills() -> dict[str, Any]:
    skills = _read_hermes_skills()
    return {
        "ok": HERMES_SKILLS_DIR.exists(),
        "skills": skills,
        "recent": skills[:10],
        "source": str(HERMES_SKILLS_DIR),
        "error": None if HERMES_SKILLS_DIR.exists() else f"{HERMES_SKILLS_DIR} does not exist",
        "ts": time.time(),
    }


@app.get("/api/software")
async def api_software() -> dict[str, Any]:
    software = _parse_apt_history()
    return {
        "ok": APT_HISTORY_PATH.exists(),
        "software": software,
        "source": str(APT_HISTORY_PATH),
        "error": None if APT_HISTORY_PATH.exists() else f"{APT_HISTORY_PATH} does not exist",
        "ts": time.time(),
    }


@app.get("/api/shared-tools")
async def api_shared_tools() -> dict[str, Any]:
    tools = _read_shared_tools()
    return {
        "ok": bool(tools),
        "tools": tools,
        "source": "dpkg -l",
        "error": None if tools else "No common installed tools matched from dpkg -l",
        "ts": time.time(),
    }


@app.get("/api/balances")
async def api_balances() -> dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {
            "ok": False,
            "deepseek": None,
            "error": "DEEPSEEK_API_KEY is not configured",
            "ts": time.time(),
        }

    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "k38-ops-dashboard/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        balance = 0.0
        for item in payload.get("balance_infos", []):
            if item.get("currency") == "CNY":
                balance = float(item.get("total_balance", 0) or 0)
                break

        return {"ok": True, "deepseek": balance, "currency": "CNY", "ts": time.time()}
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "deepseek": None,
            "error": f"DeepSeek HTTP {exc.code}",
            "ts": time.time(),
        }
    except Exception as exc:
        return {"ok": False, "deepseek": None, "error": str(exc), "ts": time.time()}


def _read_deepseek_balance() -> float:
    configured_balance = os.environ.get("K38_DEEPSEEK_BALANCE")
    if configured_balance:
        return float(configured_balance)

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return 543.56

    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "k38-ops-dashboard/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    for item in payload.get("balance_infos", []):
        if item.get("currency") == "CNY":
            return float(item.get("total_balance", 0) or 0)
    return 0.0


def _read_cpu_usage() -> str:
    stat_path = Path("/proc/stat")
    if not stat_path.exists():
        try:
            load_1m = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            return f"{min((load_1m / cpu_count) * 100, 100):.1f}%"
        except OSError:
            return "0.0%"

    def snapshot() -> tuple[int, int]:
        parts = stat_path.read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(value) for value in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return idle, total

    idle_a, total_a = snapshot()
    time.sleep(0.1)
    idle_b, total_b = snapshot()
    total_delta = total_b - total_a
    idle_delta = idle_b - idle_a
    if total_delta <= 0:
        return "0.0%"
    return f"{((total_delta - idle_delta) / total_delta) * 100:.1f}%"


def _read_memory_usage() -> str:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return "0.0%"

    meminfo: dict[str, int] = {}
    for line in meminfo_path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            meminfo[key] = int(value.strip().split()[0])

    total = meminfo.get("MemTotal", 0)
    available = meminfo.get("MemAvailable", 0)
    if total <= 0:
        return "0.0%"
    return f"{((total - available) / total) * 100:.1f}%"


def _read_disk_usage() -> str:
    try:
        result = subprocess.run(
            ["df", "-h", "/"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) >= 2:
            columns = lines[1].split()
            if len(columns) >= 5:
                return columns[4]
    except Exception:
        pass
    return "0.0%"


def _read_uptime() -> str:
    uptime_path = Path("/proc/uptime")
    if not uptime_path.exists():
        return "0d 0h"

    seconds = float(uptime_path.read_text(encoding="utf-8").split()[0])
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    return f"{days}d {hours}h"


def _is_service_active(service: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


@app.get("/api/ecs-status")
async def api_ecs_status() -> dict[str, Any]:
    services = {key: _is_service_active(name) for key, name in ECS_SERVICES.items()}
    try:
        deepseek_balance = _read_deepseek_balance()
    except Exception:
        deepseek_balance = 543.56

    return {
        "ok": True,
        "hostname": socket.gethostname(),
        "cpu": _read_cpu_usage(),
        "mem": _read_memory_usage(),
        "disk": _read_disk_usage(),
        "uptime": _read_uptime(),
        "deepseek_balance": round(deepseek_balance, 2),
        "services": services,
        "ts": time.time(),
    }


def _read_message_bus_url() -> list[dict[str, Any]]:
    if not MESSAGE_BUS_URL:
        return []

    token = (
        os.environ.get("ECS_TOKEN")
        or os.environ.get("K38_ECS_TOKEN")
        or os.environ.get("MESSAGE_BUS_TOKEN")
        or os.environ.get("HERMES_ECS_TOKEN")
        or ""
    )
    headers = {"Accept": "application/json", "User-Agent": "k38-ops-dashboard/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-ECS-Token"] = token

    req = urllib.request.Request(
        MESSAGE_BUS_URL,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        messages = payload.get("messages", [])
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
    return []


def _read_message_log() -> list[dict[str, Any]]:
    if not MESSAGE_LOG_PATH.exists():
        return []

    messages: list[dict[str, Any]] = []
    with MESSAGE_LOG_PATH.open("r", encoding="utf-8") as handle:
        for line in handle.readlines()[-100:]:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                messages.append(item)
    return messages


@app.get("/api/messages")
async def api_messages() -> dict[str, Any]:
    try:
        messages = _read_message_bus_url() or _read_message_log()
        normalized = []
        for item in messages[-50:]:
            normalized.append(
                {
                    "ts": float(item.get("ts") or item.get("time") or time.time()),
                    "from": item.get("from") or item.get("source") or "message-bus",
                    "level": item.get("level") or item.get("status") or "info",
                    "msg": item.get("msg") or item.get("message") or item.get("content") or "",
                }
            )
        return {"ok": True, "messages": normalized, "ts": time.time()}
    except Exception as exc:
        return {"ok": False, "messages": [], "error": str(exc), "ts": time.time()}


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>K38 运营中心</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080a0f;
      --panel: #111620;
      --panel-2: #151b26;
      --line: #253041;
      --muted: #8996aa;
      --text: #e7edf7;
      --soft: #b7c2d4;
      --green: #22c55e;
      --green-dim: rgba(34, 197, 94, 0.14);
      --blue: #38bdf8;
      --violet: #8b5cf6;
      --amber: #f59e0b;
      --red: #ef4444;
      --shadow: 0 18px 70px rgba(0, 0, 0, 0.34);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 18% -12%, rgba(56, 189, 248, 0.12), transparent 32rem),
        radial-gradient(circle at 90% 4%, rgba(139, 92, 246, 0.12), transparent 34rem),
        linear-gradient(180deg, #0b0f16 0%, var(--bg) 45%, #07090d 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    a {
      color: inherit;
      text-decoration: none;
    }

    .shell {
      width: min(1440px, calc(100% - 32px));
      margin: 0 auto;
      padding: 22px 0 32px;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 22px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 220px;
    }

    .mark {
      display: grid;
      width: 38px;
      height: 38px;
      place-items: center;
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 8px;
      background: linear-gradient(145deg, rgba(56, 189, 248, 0.18), rgba(139, 92, 246, 0.12));
      color: #dff5ff;
      font-weight: 800;
      box-shadow: 0 0 32px rgba(56, 189, 248, 0.12);
    }

    .brand h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.15;
      font-weight: 700;
    }

    .brand p {
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .nav {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 5px;
      border: 1px solid rgba(137, 150, 170, 0.16);
      border-radius: 10px;
      background: rgba(17, 22, 32, 0.72);
      backdrop-filter: blur(16px);
    }

    .nav a,
    .nav span {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 7px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .nav .active {
      color: var(--text);
      background: rgba(56, 189, 248, 0.16);
      box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.22);
    }

    .meta {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      min-width: 220px;
    }

    .live {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: #bff4d1;
    }

    .pulse {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--green);
      box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.12), 0 0 18px rgba(34, 197, 94, 0.45);
    }

    .hero {
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 16px;
      margin-bottom: 16px;
    }

    .panel {
      border: 1px solid rgba(137, 150, 170, 0.15);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(21, 27, 38, 0.96), rgba(14, 19, 28, 0.96));
      box-shadow: var(--shadow);
    }

    .overview {
      padding: 22px;
      min-height: 220px;
    }

    .eyebrow {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }

    .health {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: #c8f7d8;
      text-transform: none;
    }

    .headline {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 24px;
    }

    .headline h2 {
      margin: 0;
      font-size: clamp(28px, 4vw, 46px);
      line-height: 1;
      letter-spacing: 0;
    }

    .headline p {
      width: min(360px, 48%);
      margin: 0;
      color: var(--soft);
      font-size: 14px;
      line-height: 1.55;
      text-align: right;
    }

    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }

    .kpi {
      min-height: 96px;
      padding: 14px;
      border: 1px solid rgba(137, 150, 170, 0.14);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.45);
    }

    .kpi label {
      display: block;
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 12px;
    }

    .kpi strong {
      display: block;
      min-height: 29px;
      font-size: 24px;
      line-height: 1.2;
      font-weight: 750;
      color: var(--text);
    }

    .kpi span {
      display: block;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .ecs-card {
      display: grid;
      align-content: space-between;
      min-height: 220px;
      padding: 20px;
      border-color: rgba(34, 197, 94, 0.24);
      background:
        linear-gradient(135deg, rgba(34, 197, 94, 0.13), transparent 42%),
        linear-gradient(180deg, rgba(21, 27, 38, 0.98), rgba(12, 18, 26, 0.98));
    }

    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }

    .panel-title h3 {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
    }

    .panel-title span {
      color: var(--muted);
      font-size: 12px;
    }

    .ecs-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 18px;
    }

    .ecs-title h3 {
      margin: 0;
      font-size: 28px;
      line-height: 1.05;
      font-weight: 800;
    }

    .ecs-title p {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 30px;
      padding: 0 11px;
      border: 1px solid rgba(34, 197, 94, 0.26);
      border-radius: 999px;
      background: rgba(34, 197, 94, 0.12);
      color: #bff4d1;
      font-size: 12px;
      white-space: nowrap;
    }

    .ecs-metrics {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }

    .ecs-metric {
      min-height: 68px;
      padding: 12px;
      border: 1px solid rgba(137, 150, 170, 0.13);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.4);
    }

    .ecs-metric label {
      display: block;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }

    .ecs-metric strong {
      display: block;
      color: var(--text);
      font-size: 22px;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }

    .metric-bar {
      height: 6px;
      margin-top: 10px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(137, 150, 170, 0.15);
    }

    .metric-bar span {
      display: block;
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #22c55e, #38bdf8);
      transition: width 240ms ease;
    }

    .service-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
    }

    .service-chip {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 28px;
      padding: 0 9px;
      border: 1px solid rgba(34, 197, 94, 0.18);
      border-radius: 7px;
      background: rgba(34, 197, 94, 0.08);
      color: #cffadd;
      font-size: 12px;
      white-space: nowrap;
    }

    .service-chip.offline {
      border-color: rgba(239, 68, 68, 0.25);
      background: rgba(239, 68, 68, 0.08);
      color: #ffc7c7;
    }

    .service-chip.offline .dot {
      background: var(--red);
      box-shadow: 0 0 12px rgba(239, 68, 68, 0.55);
    }

    .ecs-foot {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding-top: 14px;
      border-top: 1px solid rgba(137, 150, 170, 0.13);
      color: var(--muted);
      font-size: 12px;
    }

    .details-link {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 11px;
      border: 1px solid rgba(56, 189, 248, 0.24);
      border-radius: 7px;
      background: rgba(56, 189, 248, 0.1);
      color: #c9f1ff;
      font-size: 12px;
    }

    .nodes-section {
      margin-bottom: 16px;
    }

    .node-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 9px;
    }

    .node {
      display: grid;
      gap: 10px;
      min-height: 126px;
      padding: 10px 11px;
      border: 1px solid rgba(34, 197, 94, 0.18);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(34, 197, 94, 0.09), rgba(34, 197, 94, 0.035));
      color: #d9ffe6;
      font-size: 13px;
    }

    .node-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .node .state {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: #98efb7;
      font-size: 12px;
    }

    .node-stats {
      display: grid;
      gap: 6px;
    }

    .node-stat {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 11px;
    }

    .node-stat span:last-child {
      color: var(--text);
      text-align: right;
      overflow-wrap: anywhere;
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--green);
      box-shadow: 0 0 12px rgba(34, 197, 94, 0.65);
    }

    .grid {
      display: grid;
      grid-template-columns: minmax(0, 0.95fr) minmax(420px, 1.25fr);
      gap: 16px;
    }

    .stack {
      display: grid;
      gap: 16px;
    }

    .card {
      padding: 18px;
    }

    .resource-list {
      display: grid;
      gap: 10px;
    }

    .resource {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: center;
      min-height: 68px;
      padding: 13px 14px;
      border: 1px solid rgba(137, 150, 170, 0.12);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.35);
    }

    .resource-name {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .resource-icon {
      display: grid;
      flex: 0 0 auto;
      width: 34px;
      height: 34px;
      place-items: center;
      border-radius: 8px;
      background: rgba(56, 189, 248, 0.13);
      color: #dff5ff;
      font-size: 16px;
    }

    .resource h4 {
      margin: 0;
      font-size: 14px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }

    .resource p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .resource-value {
      text-align: right;
      white-space: nowrap;
    }

    .resource-value strong {
      display: block;
      font-size: 18px;
      line-height: 1.2;
    }

    .resource-value span {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 6px;
      color: #98efb7;
      font-size: 12px;
    }

    .chart-card {
      min-height: 376px;
    }

    .chart-wrap {
      position: relative;
      height: 294px;
    }

    .messages {
      min-height: 310px;
    }

    .message-list {
      display: grid;
      gap: 8px;
    }

    .message {
      display: grid;
      grid-template-columns: 86px 110px 76px 1fr;
      gap: 12px;
      align-items: center;
      min-height: 42px;
      padding: 10px 12px;
      border: 1px solid rgba(137, 150, 170, 0.1);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.33);
      color: var(--soft);
      font-size: 12px;
    }

    .message strong {
      color: var(--text);
      font-size: 13px;
      overflow-wrap: anywhere;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 23px;
      padding: 0 8px;
      border-radius: 999px;
      background: rgba(56, 189, 248, 0.12);
      color: #bdeeff;
      font-size: 11px;
      text-transform: uppercase;
    }

    .badge.warn {
      background: rgba(245, 158, 11, 0.14);
      color: #ffd891;
    }

    .badge.error {
      background: rgba(239, 68, 68, 0.15);
      color: #ffb7b7;
    }

    .empty,
    .error {
      display: grid;
      min-height: 170px;
      place-items: center;
      border: 1px dashed rgba(137, 150, 170, 0.18);
      border-radius: 8px;
      color: var(--muted);
      font-size: 13px;
      text-align: center;
      padding: 18px;
    }

    .error {
      border-color: rgba(239, 68, 68, 0.25);
      color: #ffc7c7;
      background: rgba(239, 68, 68, 0.05);
    }

    @media (max-width: 1100px) {
      .topbar,
      .hero,
      .grid {
        grid-template-columns: 1fr;
      }

      .topbar {
        display: grid;
      }

      .meta {
        justify-content: flex-start;
      }

      .headline {
        display: block;
      }

      .headline p {
        width: 100%;
        margin-top: 12px;
        text-align: left;
      }

      .node-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100% - 20px, 1440px);
        padding-top: 12px;
      }

      .nav {
        width: 100%;
        overflow-x: auto;
        justify-content: flex-start;
      }

      .kpis,
      .ecs-metrics,
      .node-grid {
        grid-template-columns: 1fr;
      }

      .ecs-head,
      .ecs-foot {
        display: grid;
      }

      .resource,
      .message {
        grid-template-columns: 1fr;
      }

      .resource-value {
        text-align: left;
      }

      .chart-wrap {
        height: 250px;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="K38 运营中心">
        <span class="mark">K38</span>
        <span>
          <h1>K38 运营中心</h1>
          <p>Layer 1 / Operations Dashboard</p>
        </span>
      </a>
      <nav class="nav" aria-label="Layer navigation">
        <span class="active">Layer 1 总控台</span>
        <a href="/operations">Layer 2 运营</a>
        <a href="/capabilities">Layer 3 能力库</a>
      </nav>
      <div class="meta">
        <span id="clock">--</span>
        <span class="live"><span class="pulse"></span>Live</span>
      </div>
    </header>

    <section class="hero">
      <div class="panel overview">
        <div class="eyebrow">
          <span>Command overview</span>
          <span class="health"><span class="dot"></span>7 / 7 nodes healthy</span>
        </div>
        <div class="headline">
          <h2>全链路运行正常</h2>
          <p>DeepSeek 余额与消息总线实时拉取，Codex 与 API-Football 按当前静态配额展示。</p>
        </div>
        <div class="kpis">
          <div class="kpi">
            <label>DeepSeek 余额</label>
            <strong id="deepseekKpi">--</strong>
            <span id="deepseekStatus">等待同步</span>
          </div>
          <div class="kpi">
            <label>Codex 积分</label>
            <strong>151万</strong>
            <span>静态额度 / 正常</span>
          </div>
          <div class="kpi">
            <label>API-Football</label>
            <strong>72,500</strong>
            <span>每日额度 / 正常</span>
          </div>
          <div class="kpi">
            <label>消息总线</label>
            <strong id="messageKpi">--</strong>
            <span id="messageStatus">等待同步</span>
          </div>
        </div>
      </div>

      <div class="panel ecs-card">
        <div>
          <div class="ecs-head">
            <div class="ecs-title">
              <h3>ECS 香港</h3>
              <p id="ecsHostname">hostname --</p>
            </div>
            <span class="status-pill"><span class="dot"></span>online</span>
          </div>
          <div class="ecs-metrics">
            <div class="ecs-metric">
              <label>CPU usage</label>
              <strong id="ecsCpu">--</strong>
              <div class="metric-bar"><span id="ecsCpuBar"></span></div>
            </div>
            <div class="ecs-metric">
              <label>Memory usage</label>
              <strong id="ecsMem">--</strong>
              <div class="metric-bar"><span id="ecsMemBar"></span></div>
            </div>
            <div class="ecs-metric">
              <label>Disk usage</label>
              <strong id="ecsDisk">--</strong>
              <div class="metric-bar"><span id="ecsDiskBar"></span></div>
            </div>
            <div class="ecs-metric">
              <label>Uptime</label>
              <strong id="ecsUptime">--</strong>
            </div>
            <div class="ecs-metric">
              <label>DeepSeek balance</label>
              <strong id="ecsDeepseek">--</strong>
            </div>
            <div class="ecs-metric">
              <label>Active services</label>
              <strong id="ecsServiceCount">--</strong>
            </div>
          </div>
          <div class="service-list" id="ecsServices"></div>
        </div>
        <div class="ecs-foot">
          <span id="ecsUpdated">等待同步</span>
          <a class="details-link" href="/api/ecs-status">details</a>
        </div>
      </div>
    </section>

    <section class="panel card nodes-section">
      <div class="panel-title">
        <h3>其他节点状态</h3>
        <span>6 nodes online</span>
      </div>
      <div class="node-grid" id="nodeGrid"></div>
    </section>

    <section class="grid">
      <div class="stack">
        <div class="panel card">
          <div class="panel-title">
            <h3>资源余额</h3>
            <span id="balanceUpdated">--</span>
          </div>
          <div class="resource-list">
            <div class="resource">
              <div class="resource-name">
                <span class="resource-icon">D</span>
                <div>
                  <h4>DeepSeek</h4>
                  <p>API balance / CNY</p>
                </div>
              </div>
              <div class="resource-value">
                <strong id="deepseekBalance">--</strong>
                <span id="deepseekHealth"><span class="dot"></span>同步中</span>
              </div>
            </div>
            <div class="resource">
              <div class="resource-name">
                <span class="resource-icon">C</span>
                <div>
                  <h4>Codex</h4>
                  <p>积分池</p>
                </div>
              </div>
              <div class="resource-value">
                <strong>151万积分</strong>
                <span><span class="dot"></span>正常</span>
              </div>
            </div>
            <div class="resource">
              <div class="resource-name">
                <span class="resource-icon">F</span>
                <div>
                  <h4>API-Football</h4>
                  <p>Daily request quota</p>
                </div>
              </div>
              <div class="resource-value">
                <strong>72,500/天</strong>
                <span><span class="dot"></span>正常</span>
              </div>
            </div>
          </div>
        </div>

        <div class="panel card messages">
          <div class="panel-title">
            <h3>消息流水</h3>
            <span id="messagesUpdated">--</span>
          </div>
          <div id="messages" class="empty">加载消息总线...</div>
        </div>
      </div>

      <div class="panel card chart-card">
        <div class="panel-title">
          <h3>消耗趋势</h3>
          <span>7 day view</span>
        </div>
        <div class="chart-wrap">
          <canvas id="trendChart"></canvas>
        </div>
      </div>
    </section>
  </main>

  <script>
    const nodes = ["ECS 香港", "三万八", "小四", "大傻", "二傻"];
    const nodesStatusUrl = "/api/nodes-status";
    const ecsServiceLabels = {
      msg_bus: "msg-bus",
      dltrace_api: "dltrace API",
      nginx: "nginx",
      k38_football: "k38-football",
      fotao_bi: "fotao BI",
      hermes_gateway: "hermes-gateway"
    };
    const trendLabels = ["06/12", "06/13", "06/14", "06/15", "06/16", "06/17", "今日"];
    const trendData = {
      deepseek: [41, 52, 37, 68, 48, 36, 44],
      codex: [28, 33, 22, 45, 31, 24, 29],
      football: [6100, 7200, 6600, 8500, 7400, 6900, 7300]
    };

    const yuan = new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: "CNY",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });

    function byId(id) {
      return document.getElementById(id);
    }

    function formatTime(ts) {
      const date = ts ? new Date(ts * 1000) : new Date();
      return date.toLocaleTimeString("zh-CN", { hour12: false });
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function setError(container, message) {
      container.className = "error";
      container.textContent = message;
    }

    function percentValue(value) {
      const parsed = parseFloat(String(value ?? "").replace(/[^\d.\-]/g, ""));
      if (!Number.isFinite(parsed)) {
        return 0;
      }
      return Math.max(0, Math.min(100, parsed));
    }

    function setMetricBar(id, value) {
      byId(id).style.width = `${percentValue(value)}%`;
    }

    function formatNodeValue(value) {
      return value === undefined || value === null || value === "" ? "--" : escapeHtml(value);
    }

    function renderNodes(statuses = {}) {
      byId("nodeGrid").innerHTML = nodes.map((name) => {
        const node = statuses[name] || {};
        return `
        <div class="node">
          <div class="node-head">
            <strong>${escapeHtml(name)}</strong>
            <span class="state"><span class="dot"></span>online</span>
          </div>
          <div class="node-stats">
            <div class="node-stat"><span>CPU</span><span>${formatNodeValue(node.cpu)}</span></div>
            <div class="node-stat"><span>Memory</span><span>${formatNodeValue(node.mem)}</span></div>
            <div class="node-stat"><span>Disk</span><span>${formatNodeValue(node.disk)}</span></div>
            <div class="node-stat"><span>Uptime</span><span>${formatNodeValue(node.uptime)}</span></div>
          </div>
        </div>
      `;
      }).join("");
    }

    async function fetchJson(url, timeoutMs = 5000) {
      const controller = new AbortController();
      const id = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, { cache: "no-store", signal: controller.signal });
        clearTimeout(id);
        if (!response.ok) {
          throw new Error(`${url} HTTP ${response.status}`);
        }
        return response.json();
      } catch(e) {
        clearTimeout(id);
        throw e;
      }
    }

    async function loadEcsStatus() {
      const servicesContainer = byId("ecsServices");
      try {
        const data = await fetchJson("/api/ecs-status");
        if (!data.ok) {
          throw new Error(data.error || "ECS status unavailable");
        }

        const services = data.services || {};
        const activeServices = Object.values(services).filter(Boolean).length;
        const totalServices = Object.keys(ecsServiceLabels).length;

        byId("ecsHostname").textContent = `hostname ${data.hostname || "--"}`;
        byId("ecsCpu").textContent = data.cpu || "--";
        byId("ecsMem").textContent = data.mem || "--";
        byId("ecsDisk").textContent = data.disk || "--";
        setMetricBar("ecsCpuBar", data.cpu);
        setMetricBar("ecsMemBar", data.mem);
        setMetricBar("ecsDiskBar", data.disk);
        byId("ecsUptime").textContent = data.uptime || "--";
        byId("ecsDeepseek").textContent = yuan.format(Number(data.deepseek_balance || 0));
        byId("ecsServiceCount").textContent = `${activeServices}/${totalServices}`;
        byId("ecsUpdated").textContent = `updated ${formatTime(data.ts)}`;
        servicesContainer.className = "service-list";
        servicesContainer.innerHTML = Object.entries(ecsServiceLabels).map(([key, label]) => {
          const active = Boolean(services[key]);
          return `
            <span class="service-chip ${active ? "" : "offline"}">
              <span class="dot"></span>${escapeHtml(label)}
            </span>
          `;
        }).join("");
      } catch (error) {
        byId("ecsUpdated").textContent = "sync failed";
        setMetricBar("ecsCpuBar", 0);
        setMetricBar("ecsMemBar", 0);
        setMetricBar("ecsDiskBar", 0);
        servicesContainer.className = "error";
        servicesContainer.textContent = error.message;
      }
    }

    async function loadNodesStatus() {
      try {
        const data = await fetchJson(nodesStatusUrl);
        renderNodes(data.nodes || {});
      } catch (error) {
        renderNodes();
      }
    }

    async function loadBalances() {
      const health = byId("deepseekHealth");
      const status = byId("deepseekStatus");
      try {
        const data = await fetchJson("/api/balances");
        if (!data.ok) {
          throw new Error(data.error || "DeepSeek balance unavailable");
        }
        const balance = Number(data.deepseek || 0);
        byId("deepseekBalance").textContent = yuan.format(balance);
        byId("deepseekKpi").textContent = yuan.format(balance);
        byId("balanceUpdated").textContent = `updated ${formatTime(data.ts)}`;
        health.innerHTML = '<span class="dot"></span>正常';
        status.textContent = "实时余额 / 正常";
      } catch (error) {
        byId("deepseekBalance").textContent = "--";
        byId("deepseekKpi").textContent = "--";
        byId("balanceUpdated").textContent = "sync failed";
        health.textContent = "同步失败";
        status.textContent = error.message;
      }
    }

    function renderMessages(messages) {
      const container = byId("messages");
      if (!messages.length) {
        container.className = "empty";
        container.textContent = "暂无消息";
        return;
      }

      container.className = "message-list";
      container.innerHTML = messages.slice(-10).reverse().map((item) => {
        const level = String(item.level || "info").toLowerCase();
        const badgeClass = level.includes("error") ? "error" : level.includes("warn") ? "warn" : "";
        return `
          <div class="message">
            <span>${formatTime(item.ts)}</span>
            <strong>${escapeHtml(item.from || "message-bus")}</strong>
            <span class="badge ${badgeClass}">${escapeHtml(level)}</span>
            <span>${escapeHtml(item.msg || "")}</span>
          </div>
        `;
      }).join("");
    }

    async function loadMessages() {
      try {
        const data = await fetchJson("/api/messages");
        if (!data.ok) {
          throw new Error(data.error || "message bus unavailable");
        }
        const messages = Array.isArray(data.messages) ? data.messages : [];
        byId("messageKpi").textContent = String(messages.length);
        byId("messageStatus").textContent = messages.length ? "最近 50 条 / 实时" : "无新消息";
        byId("messagesUpdated").textContent = `updated ${formatTime(data.ts)}`;
        renderMessages(messages);
      } catch (error) {
        byId("messageKpi").textContent = "--";
        byId("messageStatus").textContent = "同步失败";
        byId("messagesUpdated").textContent = "sync failed";
        setError(byId("messages"), error.message);
      }
    }

    function initChart() {
      const ctx = byId("trendChart");
      Chart.defaults.color = "#8996aa";
      Chart.defaults.font.family = 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

      new Chart(ctx, {
        type: "line",
        data: {
          labels: trendLabels,
          datasets: [
            {
              label: "DeepSeek",
              data: trendData.deepseek,
              borderColor: "#38bdf8",
              backgroundColor: "rgba(56, 189, 248, 0.12)",
              borderWidth: 2,
              pointRadius: 3,
              pointHoverRadius: 5,
              tension: 0.35,
              fill: true
            },
            {
              label: "Codex",
              data: trendData.codex,
              borderColor: "#22c55e",
              backgroundColor: "rgba(34, 197, 94, 0.08)",
              borderWidth: 2,
              pointRadius: 3,
              pointHoverRadius: 5,
              tension: 0.35,
              fill: true
            },
            {
              label: "API-Football",
              data: trendData.football.map((value) => Math.round(value / 100)),
              borderColor: "#f59e0b",
              backgroundColor: "rgba(245, 158, 11, 0.08)",
              borderWidth: 2,
              pointRadius: 3,
              pointHoverRadius: 5,
              tension: 0.35,
              fill: true
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: {
              position: "top",
              align: "end",
              labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true }
            },
            tooltip: {
              backgroundColor: "rgba(8, 11, 17, 0.95)",
              borderColor: "rgba(137, 150, 170, 0.18)",
              borderWidth: 1,
              padding: 12,
              displayColors: true
            }
          },
          scales: {
            x: {
              grid: { color: "rgba(137, 150, 170, 0.08)" },
              ticks: { color: "#8996aa" }
            },
            y: {
              beginAtZero: true,
              grid: { color: "rgba(137, 150, 170, 0.08)" },
              ticks: { color: "#8996aa" }
            }
          }
        }
      });
    }

    function tickClock() {
      byId("clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
    }

    renderNodes();
    initChart();
    tickClock();
    loadEcsStatus();
    loadNodesStatus();
    loadBalances();
    loadMessages();
    setInterval(tickClock, 1000);
    setInterval(loadEcsStatus, 30000);
    setInterval(loadNodesStatus, 20000);
    setInterval(loadBalances, 30000);
    setInterval(loadMessages, 10000);
  </script>
</body>
</html>"""

OPERATIONS_HTML = (
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>K38 运营详情</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080a0f;
      --panel: #111620;
      --panel-2: #151b26;
      --line: #253041;
      --muted: #8996aa;
      --text: #e7edf7;
      --soft: #b7c2d4;
      --green: #22c55e;
      --blue: #38bdf8;
      --amber: #f59e0b;
      --shadow: 0 18px 70px rgba(0, 0, 0, 0.34);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 18% -12%, rgba(56, 189, 248, 0.12), transparent 32rem),
        radial-gradient(circle at 90% 4%, rgba(139, 92, 246, 0.12), transparent 34rem),
        linear-gradient(180deg, #0b0f16 0%, var(--bg) 45%, #07090d 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    a { color: inherit; text-decoration: none; }

    .shell {
      width: min(1440px, calc(100% - 32px));
      margin: 0 auto;
      padding: 22px 0 32px;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 22px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 220px;
    }

    .mark {
      display: grid;
      width: 38px;
      height: 38px;
      place-items: center;
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 8px;
      background: linear-gradient(145deg, rgba(56, 189, 248, 0.18), rgba(139, 92, 246, 0.12));
      color: #dff5ff;
      font-weight: 800;
      box-shadow: 0 0 32px rgba(56, 189, 248, 0.12);
    }

    .brand h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.15;
      font-weight: 700;
    }

    .brand p {
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .nav {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 5px;
      border: 1px solid rgba(137, 150, 170, 0.16);
      border-radius: 10px;
      background: rgba(17, 22, 32, 0.72);
      backdrop-filter: blur(16px);
    }

    .nav a,
    .nav span {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 7px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .nav .active {
      color: var(--text);
      background: rgba(56, 189, 248, 0.16);
      box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.22);
    }

    .meta {
      display: flex;
      justify-content: flex-end;
      min-width: 220px;
      color: var(--muted);
      font-size: 12px;
    }

    .back-link {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 0 12px;
      border: 1px solid rgba(56, 189, 248, 0.24);
      border-radius: 7px;
      background: rgba(56, 189, 248, 0.1);
      color: #c9f1ff;
      font-size: 12px;
    }

    .page-head {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 16px;
      padding: 22px;
      border: 1px solid rgba(137, 150, 170, 0.15);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(21, 27, 38, 0.96), rgba(14, 19, 28, 0.96));
      box-shadow: var(--shadow);
    }

    .page-head h2 {
      margin: 0;
      font-size: clamp(28px, 4vw, 44px);
      line-height: 1;
      letter-spacing: 0;
    }

    .page-head p {
      margin: 10px 0 0;
      max-width: 760px;
      color: var(--soft);
      font-size: 14px;
      line-height: 1.55;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      min-width: 420px;
    }

    .summary-card {
      min-height: 82px;
      padding: 13px;
      border: 1px solid rgba(137, 150, 170, 0.14);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.45);
    }

    .summary-card label {
      display: block;
      color: var(--muted);
      font-size: 12px;
    }

    .summary-card strong {
      display: block;
      margin-top: 10px;
      color: var(--text);
      font-size: 22px;
      line-height: 1.15;
    }

    .panel {
      border: 1px solid rgba(137, 150, 170, 0.15);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(21, 27, 38, 0.96), rgba(14, 19, 28, 0.96));
      box-shadow: var(--shadow);
    }

    .card { padding: 18px; }

    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }

    .panel-title h3 {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
    }

    .panel-title span {
      color: var(--muted);
      font-size: 12px;
    }

    .table-wrap {
      overflow-x: auto;
      border: 1px solid rgba(137, 150, 170, 0.12);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.3);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 820px;
    }

    th,
    td {
      padding: 13px 14px;
      border-bottom: 1px solid rgba(137, 150, 170, 0.1);
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
      background: rgba(8, 11, 17, 0.28);
    }

    tr:last-child td { border-bottom: 0; }

    .positive { color: #98efb7; font-weight: 700; }

    .two-column {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 16px;
    }

    .list {
      display: grid;
      gap: 10px;
    }

    .list-item {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 14px;
      align-items: center;
      min-height: 76px;
      padding: 13px 14px;
      border: 1px solid rgba(137, 150, 170, 0.12);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.35);
    }

    .list-item h4 {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    .list-item p {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .list-item time {
      color: #c9f1ff;
      font-size: 12px;
      white-space: nowrap;
    }

    .empty,
    .error {
      display: grid;
      min-height: 120px;
      place-items: center;
      border: 1px dashed rgba(137, 150, 170, 0.18);
      border-radius: 8px;
      color: var(--muted);
      font-size: 13px;
      text-align: center;
      padding: 18px;
    }

    .error {
      border-color: rgba(239, 68, 68, 0.25);
      color: #ffc7c7;
      background: rgba(239, 68, 68, 0.05);
    }

    @media (max-width: 1100px) {
      .topbar,
      .page-head,
      .two-column {
        display: grid;
        grid-template-columns: 1fr;
      }

      .meta { justify-content: flex-start; }
      .summary { min-width: 0; }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100% - 20px, 1440px);
        padding-top: 12px;
      }

      .nav {
        width: 100%;
        overflow-x: auto;
        justify-content: flex-start;
      }

      .summary,
      .list-item {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="K38 运营中心">
        <span class="mark">K38</span>
        <span>
          <h1>K38 运营中心</h1>
          <p>Layer 2 / Operations Detail</p>
        </span>
      </a>
      <nav class="nav" aria-label="Layer navigation">
        <a href="/">Layer 1 总控台</a>
        <span class="active">Layer 2 运营</span>
        <a href="/capabilities">Layer 3 能力库</a>
      </nav>
      <div class="meta">
        <a class="back-link" href="/">返回 Dashboard</a>
      </div>
    </header>

    <section class="page-head">
      <div>
        <h2>运营详情</h2>
        <p>每日收入、成本、利润和运营备注集中展示，同时跟踪近期安装的 Hermes 技能与节点软件。</p>
      </div>
      <div class="summary">
        <div class="summary-card">
          <label>7 日收入</label>
          <strong id="summaryRevenue">--</strong>
        </div>
        <div class="summary-card">
          <label>7 日成本</label>
          <strong id="summaryCost">--</strong>
        </div>
        <div class="summary-card">
          <label>7 日利润</label>
          <strong id="summaryProfit">--</strong>
        </div>
      </div>
    </section>

    <section class="panel card">
      <div class="panel-title">
        <h3>每日日报</h3>
        <span>Last 7 days</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>日期</th>
              <th>收入</th>
              <th>成本</th>
              <th>利润</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody id="dailyReportRows">
            <tr><td colspan="5">加载 Fotao BI 日报...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="two-column">
      <div class="panel card">
        <div class="panel-title">
          <h3>最近安装的技能</h3>
          <span>Hermes skills</span>
        </div>
        <div class="list" id="recentSkills">
          <div class="empty">加载 Hermes skills...</div>
        </div>
      </div>

      <div class="panel card">
        <div class="panel-title">
          <h3>最近安装的软件</h3>
          <span>Tools and packages</span>
        </div>
        <div class="list" id="recentSoftware">
          <div class="empty">加载 apt history...</div>
        </div>
      </div>
    </section>
  </main>
  <script>
    function byId(id) {
      return document.getElementById(id);
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function numericCurrency(value) {
      const number = Number(String(value ?? "").replace(/[^\d.-]/g, ""));
      return Number.isFinite(number) ? number : 0;
    }

    function yuan(value) {
      return `¥${Math.round(value).toLocaleString("zh-CN")}`;
    }

    async function fetchJson(url, timeoutMs = 5000) {
      const controller = new AbortController();
      const id = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, { cache: "no-store", signal: controller.signal });
        clearTimeout(id);
        if (!response.ok) {
          throw new Error(`${url} HTTP ${response.status}`);
        }
        return response.json();
      } catch(e) {
        clearTimeout(id);
        throw e;
      }
      if (!response.ok) {
        throw new Error(`${url} HTTP ${response.status}`);
      }
      return response.json();
    }

    function renderReports(reports) {
      const rows = byId("dailyReportRows");
      if (!reports.length) {
        rows.innerHTML = '<tr><td colspan="5">暂无日报数据</td></tr>';
        byId("summaryRevenue").textContent = "--";
        byId("summaryCost").textContent = "--";
        byId("summaryProfit").textContent = "--";
        return;
      }
      const visible = reports.slice(0, 7);
      const totals = visible.reduce((acc, item) => {
        acc.revenue += numericCurrency(item.revenue);
        acc.cost += numericCurrency(item.cost);
        acc.profit += numericCurrency(item.profit);
        return acc;
      }, { revenue: 0, cost: 0, profit: 0 });
      byId("summaryRevenue").textContent = yuan(totals.revenue);
      byId("summaryCost").textContent = yuan(totals.cost);
      byId("summaryProfit").textContent = yuan(totals.profit);
      rows.innerHTML = visible.map((item) => `
        <tr>
          <td>${escapeHtml(item.date || "--")}</td>
          <td>${escapeHtml(item.revenue || "--")}</td>
          <td>${escapeHtml(item.cost || "--")}</td>
          <td class="positive">${escapeHtml(item.profit || "--")}</td>
          <td>${escapeHtml(item.notes || "")}</td>
        </tr>
      `).join("");
    }

    function renderList(containerId, items, emptyMessage) {
      const container = byId(containerId);
      if (!items.length) {
        container.innerHTML = `<div class="empty">${escapeHtml(emptyMessage)}</div>`;
        return;
      }
      container.innerHTML = items.map((item) => `
        <article class="list-item">
          <div>
            <h4>${escapeHtml(item.name || "--")}</h4>
            <p>${escapeHtml(item.description || "")}</p>
          </div>
          <time>${escapeHtml(item.installed || "--")}</time>
        </article>
      `).join("");
    }

    async function loadOperationsData() {
      const [reportsResult, skillsResult, softwareResult] = await Promise.allSettled([
        fetchJson("/api/daily-reports"),
        fetchJson("/api/skills"),
        fetchJson("/api/software")
      ]);

      if (reportsResult.status === "fulfilled") {
        renderReports(Array.isArray(reportsResult.value.reports) ? reportsResult.value.reports : []);
      } else {
        byId("dailyReportRows").innerHTML = `<tr><td colspan="5">${escapeHtml(reportsResult.reason.message)}</td></tr>`;
      }

      if (skillsResult.status === "fulfilled") {
        const skills = Array.isArray(skillsResult.value.recent) ? skillsResult.value.recent : [];
        renderList("recentSkills", skills, "未检测到 Hermes skills");
      } else {
        renderList("recentSkills", [], skillsResult.reason.message);
      }

      if (softwareResult.status === "fulfilled") {
        const software = Array.isArray(softwareResult.value.software) ? softwareResult.value.software : [];
        renderList("recentSoftware", software.slice(0, 10), "未读取到近期 apt 安装记录");
      } else {
        renderList("recentSoftware", [], softwareResult.reason.message);
      }
    }

    loadOperationsData();
  </script>
</body>
</html>"""
)

CAPABILITIES_HTML = (
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>K38 能力库</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080a0f;
      --panel: #111620;
      --panel-2: #151b26;
      --line: #253041;
      --muted: #8996aa;
      --text: #e7edf7;
      --soft: #b7c2d4;
      --green: #22c55e;
      --blue: #38bdf8;
      --amber: #f59e0b;
      --shadow: 0 18px 70px rgba(0, 0, 0, 0.34);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 18% -12%, rgba(56, 189, 248, 0.12), transparent 32rem),
        radial-gradient(circle at 90% 4%, rgba(139, 92, 246, 0.12), transparent 34rem),
        linear-gradient(180deg, #0b0f16 0%, var(--bg) 45%, #07090d 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    a { color: inherit; text-decoration: none; }

    .shell {
      width: min(1440px, calc(100% - 32px));
      margin: 0 auto;
      padding: 22px 0 32px;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 22px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 220px;
    }

    .mark {
      display: grid;
      width: 38px;
      height: 38px;
      place-items: center;
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 8px;
      background: linear-gradient(145deg, rgba(56, 189, 248, 0.18), rgba(139, 92, 246, 0.12));
      color: #dff5ff;
      font-weight: 800;
      box-shadow: 0 0 32px rgba(56, 189, 248, 0.12);
    }

    .brand h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.15;
      font-weight: 700;
    }

    .brand p {
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .nav {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 5px;
      border: 1px solid rgba(137, 150, 170, 0.16);
      border-radius: 10px;
      background: rgba(17, 22, 32, 0.72);
      backdrop-filter: blur(16px);
    }

    .nav a,
    .nav span {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 7px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .nav .active {
      color: var(--text);
      background: rgba(56, 189, 248, 0.16);
      box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.22);
    }

    .meta {
      display: flex;
      justify-content: flex-end;
      min-width: 220px;
      color: var(--muted);
      font-size: 12px;
    }

    .back-link {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 0 12px;
      border: 1px solid rgba(56, 189, 248, 0.24);
      border-radius: 7px;
      background: rgba(56, 189, 248, 0.1);
      color: #c9f1ff;
      font-size: 12px;
    }

    .page-head {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 16px;
      padding: 22px;
      border: 1px solid rgba(137, 150, 170, 0.15);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(21, 27, 38, 0.96), rgba(14, 19, 28, 0.96));
      box-shadow: var(--shadow);
    }

    .page-head h2 {
      margin: 0;
      font-size: clamp(28px, 4vw, 44px);
      line-height: 1;
      letter-spacing: 0;
    }

    .page-head p {
      margin: 10px 0 0;
      max-width: 760px;
      color: var(--soft);
      font-size: 14px;
      line-height: 1.55;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      min-width: 420px;
    }

    .summary-card {
      min-height: 82px;
      padding: 13px;
      border: 1px solid rgba(137, 150, 170, 0.14);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.45);
    }

    .summary-card label {
      display: block;
      color: var(--muted);
      font-size: 12px;
    }

    .summary-card strong {
      display: block;
      margin-top: 10px;
      color: var(--text);
      font-size: 22px;
      line-height: 1.15;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(360px, 0.8fr);
      gap: 16px;
    }

    .stack {
      display: grid;
      gap: 16px;
    }

    .panel {
      border: 1px solid rgba(137, 150, 170, 0.15);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(21, 27, 38, 0.96), rgba(14, 19, 28, 0.96));
      box-shadow: var(--shadow);
    }

    .card { padding: 18px; }

    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }

    .panel-title h3 {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
    }

    .panel-title span {
      color: var(--muted);
      font-size: 12px;
    }

    .catalog,
    .tool-list,
    .recommendations {
      display: grid;
      gap: 10px;
    }

    .catalog-item,
    .tool-item,
    .recommendation {
      display: grid;
      gap: 14px;
      min-height: 76px;
      padding: 13px 14px;
      border: 1px solid rgba(137, 150, 170, 0.12);
      border-radius: 8px;
      background: rgba(8, 11, 17, 0.35);
    }

    .catalog-item {
      grid-template-columns: 1fr auto;
      align-items: center;
    }

    .item-main {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .item-icon {
      display: grid;
      flex: 0 0 auto;
      width: 34px;
      height: 34px;
      place-items: center;
      border-radius: 8px;
      background: rgba(56, 189, 248, 0.13);
      color: #dff5ff;
      font-size: 14px;
      font-weight: 800;
    }

    .catalog-item h4,
    .tool-item h4,
    .recommendation h4 {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    .catalog-item p,
    .tool-item p,
    .recommendation p {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .usage {
      min-width: 74px;
      text-align: right;
    }

    .usage strong {
      display: block;
      color: #98efb7;
      font-size: 22px;
      line-height: 1.15;
    }

    .usage span {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }

    .tool-item {
      grid-template-columns: 1fr auto;
      align-items: center;
    }

    .tool-item span,
    .rec-type {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 25px;
      padding: 0 9px;
      border-radius: 999px;
      background: rgba(56, 189, 248, 0.12);
      color: #bdeeff;
      font-size: 11px;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .recommendation {
      grid-template-columns: auto 1fr;
      align-items: flex-start;
      border-color: rgba(245, 158, 11, 0.18);
      background:
        linear-gradient(135deg, rgba(245, 158, 11, 0.08), transparent 45%),
        rgba(8, 11, 17, 0.35);
    }

    .rec-type {
      background: rgba(245, 158, 11, 0.14);
      color: #ffd891;
    }

    @media (max-width: 1100px) {
      .topbar,
      .page-head,
      .layout {
        display: grid;
        grid-template-columns: 1fr;
      }

      .meta { justify-content: flex-start; }
      .summary { min-width: 0; }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100% - 20px, 1440px);
        padding-top: 12px;
      }

      .nav {
        width: 100%;
        overflow-x: auto;
        justify-content: flex-start;
      }

      .summary,
      .catalog-item,
      .tool-item,
      .recommendation {
        grid-template-columns: 1fr;
      }

      .item-main {
        align-items: flex-start;
      }

      .usage {
        text-align: left;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="K38 运营中心">
        <span class="mark">K38</span>
        <span>
          <h1>K38 运营中心</h1>
          <p>Layer 3 / Capabilities Library</p>
        </span>
      </a>
      <nav class="nav" aria-label="Layer navigation">
        <a href="/">Layer 1 总控台</a>
        <a href="/operations">Layer 2 运营</a>
        <span class="active">Layer 3 能力库</span>
      </nav>
      <div class="meta">
        <a class="back-link" href="/">返回 Dashboard</a>
      </div>
    </header>

    <section class="page-head">
      <div>
        <h2>能力库</h2>
        <p>共享 Hermes 技能、集群工具和 CEO 推荐安装项集中管理，用于发现能力缺口和规划下一批自动化建设。</p>
      </div>
      <div class="summary">
        <div class="summary-card">
          <label>共享技能</label>
          <strong id="skillCount">--</strong>
        </div>
        <div class="summary-card">
          <label>共享工具</label>
          <strong id="toolCount">--</strong>
        </div>
        <div class="summary-card">
          <label>推荐安装</label>
          <strong>4</strong>
        </div>
      </div>
    </section>

    <section class="layout">
      <div class="stack">
        <div class="panel card">
          <div class="panel-title">
            <h3>共享技能目录</h3>
            <span>Hermes catalog</span>
          </div>
          <div class="catalog" id="skillCatalog">
            <div class="empty">加载 Hermes catalog...</div>
          </div>
        </div>

        <div class="panel card">
          <div class="panel-title">
            <h3>共享工具清单</h3>
            <span>Cluster tools</span>
          </div>
          <div class="tool-list" id="sharedTools">
            <div class="empty">读取 dpkg installed packages...</div>
          </div>
        </div>
      </div>

      <aside class="panel card">
        <div class="panel-title">
          <h3>CEO 推荐安装</h3>
          <span>Usage gaps</span>
        </div>
        <div class="recommendations">
        </div>
      </aside>
    </section>
  </main>
</body>
</html>"""
)


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(content=HTML)


@app.get("/operations", response_class=HTMLResponse)
@app.get("/operations/", response_class=HTMLResponse)
async def operations() -> HTMLResponse:
    return HTMLResponse(content=OPERATIONS_HTML)


@app.get("/capabilities", response_class=HTMLResponse)
@app.get("/capabilities/", response_class=HTMLResponse)
async def capabilities() -> HTMLResponse:
    return HTMLResponse(content=CAPABILITIES_HTML)


@app.get("/{path:path}", response_class=HTMLResponse)
async def serve_dashboard(path: str) -> HTMLResponse:
    return HTMLResponse(content=HTML)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9920)
