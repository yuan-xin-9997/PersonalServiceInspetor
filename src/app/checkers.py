from __future__ import annotations

import asyncio
import json
import re
import socket
import ssl
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import paramiko

from .config import env_value
from .redaction import redact
from .schemas import CheckResult

LOG_PATTERNS = {
    "error": re.compile(r"\bERROR\b", re.IGNORECASE),
    "warning": re.compile(r"\bWARN(?:ING)?\b", re.IGNORECASE),
    "critical": re.compile(r"\b(?:CRITICAL|FATAL)\b", re.IGNORECASE),
    "traceback": re.compile(r"Traceback \(most recent call last\):"),
    "timeout": re.compile(r"\btime(?:d?\s*out|out)\b", re.IGNORECASE),
    "connection_refused": re.compile(r"connection refused", re.IGNORECASE),
    "database_error": re.compile(r"(?:database|postgres|mysql|sqlite).{0,30}(?:error|failed|exception)", re.IGNORECASE),
    "http_4xx_5xx": re.compile(r"\b(?:HTTP[/ ]\S+\s+)?[45]\d\d\b"),
}

TIMESTAMP_PATTERNS = [
    re.compile(r"(?P<stamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
    re.compile(r"(?P<stamp>\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2})"),
]


def now_in(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def json_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(path)
    return current


def evaluate_json_assertions(data: Any, assertions: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for assertion in assertions:
        path = str(assertion["path"])
        try:
            value = json_path(data, path)
        except (KeyError, IndexError, ValueError, TypeError):
            failures.append(f"JSON 路径不存在: {path}")
            continue
        if "equals" in assertion and value != assertion["equals"]:
            failures.append(f"{path} 期望 {assertion['equals']!r}，实际 {value!r}")
        if "contains" in assertion and str(assertion["contains"]) not in str(value):
            failures.append(f"{path} 不包含 {assertion['contains']!r}")
        if "max_age_seconds" in assertion:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                age = datetime.now(parsed.tzinfo) - parsed
                if age.total_seconds() > float(assertion["max_age_seconds"]):
                    failures.append(f"{path} 数据已超过时效阈值")
            except (TypeError, ValueError):
                failures.append(f"{path} 不是有效时间")
    return failures


def parse_log_window(text: str, since: datetime) -> dict[str, Any]:
    """Count matching log lines in a time window when a timestamp is recognizable."""
    counts: Counter[str] = Counter()
    included = 0
    timestamped = 0
    samples: Counter[str] = Counter()
    for raw_line in text.splitlines():
        line = str(redact(raw_line))
        stamp: datetime | None = None
        for pattern in TIMESTAMP_PATTERNS:
            match = pattern.search(line)
            if match:
                try:
                    stamp = datetime.fromisoformat(match.group("stamp").replace("/", "-").replace(",", ".").replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=since.tzinfo)
                    timestamped += 1
                except ValueError:
                    pass
                break
        if stamp is not None and stamp < since:
            continue
        included += 1
        matched = False
        for name, pattern in LOG_PATTERNS.items():
            if pattern.search(line):
                counts[name] += 1
                matched = True
        if matched:
            signature = re.sub(r"\b\d+\b", "#", line.strip())[:180]
            samples[signature] += 1
    return {
        "lines": included,
        "bytes": len(text.encode("utf-8", errors="ignore")),
        "timestamped_lines": timestamped,
        "counts": dict(counts),
        "top_errors": [{"message": item, "count": count} for item, count in samples.most_common(5)],
    }


class CheckRunner:
    def __init__(self, defaults: dict[str, Any], timezone: str, client: httpx.AsyncClient | None = None):
        self.defaults = defaults
        self.timezone = timezone
        self._client = client

    async def run(self, item: dict[str, Any]) -> CheckResult:
        started = time.perf_counter()
        check_type = item["type"]
        try:
            handler = getattr(self, f"check_{check_type}")
        except AttributeError:
            return self._result(item, "UNKNOWN", f"不支持的检查类型: {check_type}", started)
        try:
            result = await handler(item, started)
            result.message = str(redact(result.message))
            result.metrics = redact(result.metrics)
            return result
        except Exception as exc:
            return self._result(item, "CRITICAL", f"检查异常: {redact(str(exc))}", started)

    def _result(
        self,
        item: dict[str, Any],
        status: str,
        message: str,
        started: float,
        metrics: dict[str, Any] | None = None,
    ) -> CheckResult:
        return CheckResult(
            check_id=item["id"],
            name=item["name"],
            category=item["category"],
            check_type=item["type"],
            status=status,
            message=message,
            observed_at=now_in(self.timezone),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            metrics=metrics or {},
        )

    async def check_tcp(self, item: dict[str, Any], started: float) -> CheckResult:
        timeout = float(item.get("timeout_seconds", self.defaults.get("timeout_seconds", 8)))
        connection = asyncio.open_connection(str(item["host"]), int(item["port"]))
        reader, writer = await asyncio.wait_for(connection, timeout=timeout)
        del reader
        writer.close()
        await writer.wait_closed()
        return self._result(item, "OK", f"TCP {item['host']}:{item['port']} 可连接", started)

    async def check_http(self, item: dict[str, Any], started: float) -> CheckResult:
        timeout = float(item.get("timeout_seconds", self.defaults.get("timeout_seconds", 8)))
        verify = bool(item.get("verify_tls", True))
        headers = {str(k): str(v) for k, v in item.get("headers", {}).items()}
        # Inspection targets include private RFC1918 addresses.  A proxy inherited
        # by the container must never turn those checks into proxy-generated 502s.
        client = self._client or httpx.AsyncClient(
            timeout=timeout,
            verify=verify,
            follow_redirects=True,
            trust_env=False,
        )
        close_client = self._client is None
        try:
            response = await client.request(str(item.get("method", "GET")), str(item["url"]), headers=headers)
        finally:
            if close_client:
                await client.aclose()
        elapsed_ms = (time.perf_counter() - started) * 1000
        expected = item.get("expected_status", [200])
        if response.status_code not in expected:
            return self._result(item, "CRITICAL", f"HTTP {response.status_code}，期望 {expected}", started, {"http_status": response.status_code})
        failures: list[str] = []
        keyword = item.get("body_contains")
        if keyword and str(keyword) not in response.text:
            failures.append("响应正文缺少预期关键字")
        assertions = item.get("json_assertions", [])
        if assertions:
            try:
                failures.extend(evaluate_json_assertions(response.json(), assertions))
            except ValueError:
                failures.append("响应不是有效 JSON")
        if failures:
            return self._result(item, "CRITICAL", "；".join(failures), started, {"http_status": response.status_code})
        status = "OK"
        threshold = float(item.get("warning_response_ms", self.defaults.get("warning_response_ms", 3000)))
        message = f"HTTP {response.status_code}，响应 {elapsed_ms:.0f}ms"
        if elapsed_ms >= threshold:
            status = "WARNING"
            message += f"，超过 {threshold:.0f}ms 阈值"
        metrics: dict[str, Any] = {"http_status": response.status_code, "response_ms": round(elapsed_ms, 2)}
        parsed = urlparse(str(item["url"]))
        if parsed.scheme == "https" and verify:
            try:
                tls_days = await asyncio.to_thread(self._tls_days, parsed.hostname or "", parsed.port or 443, timeout)
                metrics["tls_days_remaining"] = tls_days
                warning_days = int(self.defaults.get("tls_warning_days", 30))
                critical_days = int(self.defaults.get("tls_critical_days", 7))
                if tls_days < critical_days:
                    status = "CRITICAL"
                    message += f"，TLS 仅剩 {tls_days} 天"
                elif tls_days < warning_days and status == "OK":
                    status = "WARNING"
                    message += f"，TLS 仅剩 {tls_days} 天"
            except Exception as exc:
                metrics["tls_error"] = str(redact(str(exc)))
        return self._result(item, status, message, started, metrics)

    async def check_tls(self, item: dict[str, Any], started: float) -> CheckResult:
        days = await asyncio.to_thread(
            self._tls_days,
            str(item["host"]),
            int(item.get("port", 443)),
            float(item.get("timeout_seconds", self.defaults.get("timeout_seconds", 8))),
        )
        critical = int(item.get("critical_days", self.defaults.get("tls_critical_days", 7)))
        warning = int(item.get("warning_days", self.defaults.get("tls_warning_days", 30)))
        status = "CRITICAL" if days < critical else "WARNING" if days < warning else "OK"
        return self._result(item, status, f"TLS 证书剩余 {days} 天", started, {"tls_days_remaining": days})

    @staticmethod
    def _tls_days(host: str, port: int, timeout: float) -> int:
        context = ssl.create_default_context()
        with (
            socket.create_connection((host, port), timeout=timeout) as raw,
            context.wrap_socket(raw, server_hostname=host) as secured,
        ):
            cert = secured.getpeercert()
        expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=ZoneInfo("UTC"))
        return max(0, (expires - datetime.now(ZoneInfo("UTC"))).days)

    def _ssh_client(self, item: dict[str, Any]) -> paramiko.SSHClient:
        username = env_value(item, "username")
        password = env_value(item, "password")
        key_path = env_value(item, "key_path")
        if not username:
            raise MissingCredential("缺少 SSH 用户名环境变量")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=str(item["host"]),
            port=int(item.get("port", 22)),
            username=str(username),
            password=str(password) if password else None,
            key_filename=str(key_path) if key_path else None,
            timeout=float(item.get("timeout_seconds", self.defaults.get("timeout_seconds", 8))),
            banner_timeout=8,
            auth_timeout=8,
        )
        return client

    def _ssh_exec(self, item: dict[str, Any], command: str, limit: int = 5_242_880) -> tuple[str, str, int]:
        client = self._ssh_client(item)
        try:
            _, stdout, stderr = client.exec_command(command, timeout=20)
            out = stdout.read(limit + 1).decode("utf-8", errors="replace")
            err = stderr.read(65537).decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            if len(out) > limit:
                out = out[:limit]
            return out, err[:65536], code
        finally:
            client.close()

    async def check_host_ssh(self, item: dict[str, Any], started: float) -> CheckResult:
        command = "export LC_ALL=C; printf '__TOP__\\n'; top -bn1 | head -5; printf '__UPTIME__\\n'; uptime; printf '__MEMORY__\\n'; free -b; printf '__DISK__\\n'; df -P -x tmpfs -x devtmpfs -x iso9660 -x squashfs; printf '__INODE__\\n'; df -Pi -x tmpfs -x devtmpfs -x iso9660 -x squashfs; printf '__UPTIME_SECONDS__\\n'; cat /proc/uptime; printf '__JOURNAL__\\n'; journalctl --since '24 hours ago' -p 0..3 --no-pager -n 30 2>/dev/null || true"
        try:
            output, error, code = await asyncio.to_thread(self._ssh_exec, item, command, 524288)
        except MissingCredential as exc:
            return self._result(item, "UNKNOWN", str(exc), started)
        if code != 0:
            return self._result(item, "CRITICAL", f"SSH 采集失败: {error or code}", started)
        disk_part = output.split("__DISK__", 1)[-1].split("__INODE__", 1)[0]
        inode_part = output.split("__INODE__", 1)[-1].split("__UPTIME_SECONDS__", 1)[0]
        disk_values = [int(value) for value in re.findall(r"\s(\d+)%\s+", disk_part)]
        inode_values = [int(value) for value in re.findall(r"\s(\d+)%\s+/", inode_part)]
        max_disk = max(disk_values, default=0)
        max_inode = max(inode_values, default=0)
        idle_match = re.search(r"([\d.]+)\s*id", output)
        cpu_percent = round(100 - float(idle_match.group(1)), 2) if idle_match else None
        memory_part = output.split("__MEMORY__", 1)[-1].split("__DISK__", 1)[0]
        memory_match = re.search(r"^Mem:\s+(\d+)\s+(\d+)", memory_part, re.MULTILINE)
        memory_percent = round(int(memory_match.group(2)) / int(memory_match.group(1)) * 100, 2) if memory_match and int(memory_match.group(1)) else None
        load_match = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", output)
        loads = [float(load_match.group(index)) for index in range(1, 4)] if load_match else []
        uptime_part = output.split("__UPTIME_SECONDS__", 1)[-1].split("__JOURNAL__", 1)[0]
        uptime_match = re.search(r"([\d.]+)", uptime_part)
        uptime_seconds = int(float(uptime_match.group(1))) if uptime_match else None
        critical = int(self.defaults.get("disk_critical_percent", 90))
        warning = int(self.defaults.get("disk_warning_percent", 80))
        status = "CRITICAL" if max_disk >= critical else "WARNING" if max_disk >= warning or max_inode >= int(self.defaults.get("inode_warning_percent", 80)) else "OK"
        journal_count = sum(1 for line in output.splitlines() if re.search(r"(?:oom|I/O error|read-only file system|kernel panic)", line, re.I))
        if journal_count and status == "OK":
            status = "WARNING"
        return self._result(
            item,
            status,
            f"SSH 正常；CPU {cpu_percent if cpu_percent is not None else '-'}%；内存 {memory_percent if memory_percent is not None else '-'}%；磁盘最高 {max_disk}%；inode 最高 {max_inode}%；关键系统日志 {journal_count} 条",
            started,
            {"cpu_percent": cpu_percent, "memory_percent": memory_percent, "load_average": loads, "disk_max_percent": max_disk, "inode_max_percent": max_inode, "uptime_seconds": uptime_seconds, "rebooted_within_24h": uptime_seconds is not None and uptime_seconds < 86400, "system_error_count": journal_count, "sample": output[:4000]},
        )

    async def check_docker_ssh(self, item: dict[str, Any], started: float) -> CheckResult:
        command = "docker ps -a --format '{{json .}}'; ids=$(docker ps -aq); if [ -n \"$ids\" ]; then docker inspect --format 'INSPECT|{{.Name}}|{{.RestartCount}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' $ids; fi; docker stats --no-stream --format '{{json .}}' 2>/dev/null || true"
        try:
            output, error, code = await asyncio.to_thread(self._ssh_exec, item, command, 1048576)
        except MissingCredential as exc:
            return self._result(item, "UNKNOWN", str(exc), started)
        if code != 0:
            return self._result(item, "CRITICAL", f"Docker 采集失败: {error or code}", started)
        objects = []
        inspections = []
        for line in output.splitlines():
            if line.startswith("INSPECT|"):
                _, name, restarts, started_at, health = (line.split("|", 4) + [""] * 5)[:5]
                inspections.append({"name": name.lstrip("/"), "restart_count_total": int(restarts or 0), "started_at": started_at, "health": health or None})
                continue
            try:
                objects.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        unhealthy = [obj.get("Names") or obj.get("Name") for obj in objects if re.search(r"unhealthy|restarting|exited", str(obj.get("Status", "")), re.I)]
        status = "CRITICAL" if unhealthy else "OK"
        return self._result(item, status, f"发现 {len(inspections) or len(objects)} 个容器；异常容器 {len(unhealthy)} 个", started, {"items": objects[:200], "containers": inspections[:200], "unhealthy": unhealthy})

    async def check_log_ssh(self, item: dict[str, Any], started: float) -> CheckResult:
        paths = item.get("paths", [])
        if not paths:
            return self._result(item, "UNKNOWN", "未配置日志路径", started)
        max_bytes = int(item.get("max_log_bytes", self.defaults.get("max_log_bytes", 5_242_880)))
        quoted_paths = " ".join(str(path).replace("'", "") for path in paths)
        command = f"find {quoted_paths} -type f -mmin -1440 -print0 2>/dev/null | xargs -0 -r tail -c {max_bytes} 2>/dev/null | tail -c {max_bytes}"
        try:
            output, error, code = await asyncio.to_thread(self._ssh_exec, item, command, max_bytes)
        except MissingCredential as exc:
            return self._result(item, "UNKNOWN", str(exc), started)
        if code not in {0, 123} and not output:
            return self._result(item, "UNKNOWN", f"日志采集失败: {error or code}", started)
        stats = parse_log_window(output, now_in(self.timezone) - timedelta(hours=24))
        errors = stats["counts"].get("error", 0) + stats["counts"].get("critical", 0) + stats["counts"].get("traceback", 0)
        if not output and item.get("expect_growth"):
            status, message = "WARNING", "过去 24 小时没有可读取的新增日志"
        elif errors:
            status, message = "WARNING", f"过去 24 小时识别 {errors} 条错误/严重/Traceback"
        else:
            status, message = "OK", f"过去 24 小时抽样 {stats['lines']} 行，未识别严重异常"
        return self._result(item, status, message, started, stats)

    async def check_postgres(self, item: dict[str, Any], started: float) -> CheckResult:
        try:
            import psycopg

            params = self._database_params(item)
            connection = await psycopg.AsyncConnection.connect(**params, connect_timeout=8)
            async with connection:
                await connection.set_read_only(True)
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT current_database(), pg_database_size(current_database())")
                    database_name, size = await cursor.fetchone()
                    await cursor.execute("SELECT count(*) FROM pg_stat_activity")
                    connections = (await cursor.fetchone())[0]
                    await cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE xact_start < now() - interval '10 minutes'")
                    long_transactions = (await cursor.fetchone())[0]
                    await cursor.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
                    lock_waits = (await cursor.fetchone())[0]
            status = "WARNING" if long_transactions or lock_waits else "OK"
            return self._result(item, status, f"PostgreSQL 查询成功；连接 {connections}；长事务 {long_transactions}；锁等待 {lock_waits}", started, {"database": database_name, "size_bytes": size, "connections": connections, "long_transactions": long_transactions, "lock_waits": lock_waits})
        except MissingCredential as exc:
            return self._result(item, "UNKNOWN", str(exc), started)

    async def check_mysql(self, item: dict[str, Any], started: float) -> CheckResult:
        try:
            import pymysql

            params = self._database_params(item)
            params["database"] = params.pop("dbname")
            connection = await asyncio.to_thread(pymysql.connect, **params, connect_timeout=8, read_timeout=8, autocommit=False)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                    cursor.execute("SHOW STATUS LIKE 'Threads_connected'")
                    connections = int(cursor.fetchone()[1])
                    cursor.execute("SELECT DATABASE()")
                    database_name = cursor.fetchone()[0]
                    cursor.execute("SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables WHERE table_schema = DATABASE()")
                    size = int(cursor.fetchone()[0])
                    connection.rollback()
            finally:
                connection.close()
            return self._result(item, "OK", f"MySQL 查询成功；连接 {connections}", started, {"database": database_name, "size_bytes": size, "connections": connections})
        except MissingCredential as exc:
            return self._result(item, "UNKNOWN", str(exc), started)

    def _database_params(self, item: dict[str, Any]) -> dict[str, Any]:
        values = {
            "host": env_value(item, "host"),
            "port": env_value(item, "port"),
            "user": env_value(item, "username"),
            "password": env_value(item, "password"),
            "dbname": env_value(item, "database"),
        }
        missing = [key for key, value in values.items() if value in {None, ""}]
        if missing:
            raise MissingCredential(f"缺少数据库环境变量: {', '.join(missing)}")
        values["port"] = int(values["port"])
        return values

    async def check_jenkins(self, item: dict[str, Any], started: float) -> CheckResult:
        url = env_value(item, "url")
        username = env_value(item, "username")
        password = env_value(item, "password")
        if not url or not username or not password:
            return self._result(item, "UNKNOWN", "缺少 Jenkins URL、用户名或 API Token 环境变量", started)
        query = "api/json?tree=jobs[name,color,lastBuild[number,result,timestamp,duration,building],lastSuccessfulBuild[timestamp]]"
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(f"{str(url).rstrip('/')}/{query}", auth=(str(username), str(password)))
            response.raise_for_status()
            jobs = response.json().get("jobs", [])
        failed = [job.get("name") for job in jobs if job.get("color", "").startswith(("red", "yellow", "aborted"))]
        now_ms = int(time.time() * 1000)
        long_running = [job.get("name") for job in jobs if job.get("lastBuild", {}).get("building") and now_ms - int(job.get("lastBuild", {}).get("timestamp", now_ms)) > 2 * 60 * 60 * 1000]
        status = "WARNING" if failed or long_running else "OK"
        return self._result(item, status, f"Jenkins 共 {len(jobs)} 个任务；失败 {len(failed)} 个；长时间运行 {len(long_running)} 个", started, {"jobs": jobs, "failed_jobs": failed, "long_running_jobs": long_running})


class MissingCredential(RuntimeError):
    pass
