#!/usr/bin/env python3
"""Сборка C++-ядра sqli_core через pybind11.
Запуск: python build_sqli_core.py
Результат: sqli_core.<abi>.so рядом со скриптом."""

import sys
import subprocess

def main():
 try:
     import pybind11
 except ImportError:
     print("[*] Устанавливаю pybind11...")
     subprocess.check_call([sys.executable, "-m", "pip", "install", "pybind11"])
     import pybind11

 include = pybind11.get_include()
 ext = ".pyd" if sys.platform == "win32" else ".so"
 cmd = [
 "g++", "-O3", "-Wall", "-shared", "-std=c++17", "-fPIC",
 f"-I{include}",
 "sqli_core.cpp",
 "-o", f"sqli_core{ext}",
 ]
 if sys.platform == "darwin":
     cmd.insert(1, "-undefined dynamic_lookup")
     print("[*] Компиляция:", " ".join(cmd))
     subprocess.check_call(cmd)
     print(f"[+] Готово: sqli_core{ext}")
     # smoke-тест
     sys.path.insert(0, ".")
     import sqli_core
     m = sqli_core.mutate("' AND 1=1-- -", 4)
     d = sqli_core.diff_score("<html><body>ok</body></html>",
     "<html><body>ok</body></html>")
     print(f"[+] Smoke: {len(m)} мутаций, diff={d:.2f}")

if __name__ == "__main__":
 main()