"""GoatCounter 집계 → 팀 공유용 통계 페이지.

    python tools/stats.py            # .cache/stats/에 stats.json + eclore-stats.html, 터미널에 요약
    python tools/stats.py --sync     # 공개 통계 레포(config.yaml stats.repo)에 이 스크립트·템플릿·채널표·글꼴을 올린다

공개 레포(stats.sogangfiat.com)에서는 GitHub Actions가 매시간 이 파일을 그대로 실행한다 (표준 라이브러리만,
채널표는 stats_config.json, 글꼴은 미리 줄인 fonts/*.woff2). 채널을 바꾸거나 이 파일·템플릿을 고치면 --sync.

토큰: 환경변수 GOATCOUNTER_TOKEN 또는 ~/.config/eclore/goatcounter.token
(GoatCounter → 계정 메뉴 → API, 권한은 "Read statistics"만). 토큰은 레포에 두지 않는다 (공개 레포는 Actions 비밀값).
예매 인원: 환경변수 BOOKING_STATS_URL 또는 ~/.config/eclore/booking_stats.url (읽기 전용 Apps Script 웹앱,
tools/apps-script/booking-stats.gs — 집계 숫자만 준다). 없거나 실패하면 예매 섹션만 빠진다.

채널 이름·분류는 config.yaml 등록부에서 가져온다. 코드는 대소문자를 합치고,
링크 끝에 붙은 군더더기(줄바꿈·이모지)는 떼고, 오타 주소(/?ret=MS 등)는 그 코드로 합친다.
숫자는 GoatCounter의 방문자 수(같은 사람이 같은 날 여러 번 와도 1)이고, 광고 차단 사용자가 빠진 최소값이다.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import io
import json
import os
import re
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
IN_REPO = (HERE / "registry.py").exists() and (HERE.parent / "config.yaml").exists()
ROOT = HERE.parent if IN_REPO else HERE
OUT = ROOT / ".cache" / "stats"
TEMPLATE = HERE / "stats_page.html"
TOKEN_FILE = Path.home() / ".config" / "eclore" / "goatcounter.token"
BOOKING_URL_FILE = Path.home() / ".config" / "eclore" / "booking_stats.url"
KST = datetime.timezone(datetime.timedelta(hours=9))
WEIGHTS = ((300, "Light"), (400, "Regular"), (600, "SemiBold"))

START = "2026-10-08"   # teaser 배포·포스터 QR 게시
# 합쳐서 지운 코드 → 지금 코드 (2026-11-13 에타, 11-18 외부 DM)
ALIASES = {"ES": "ET", "EY": "ET", "EE": "ET", "NM": "DM", "GM": "DM", "AM": "DM"}
# 그래프에 표시할 날
MARKS = [("2026-10-30", "사이트 공개 · 인스타 업로드"), ("2026-11-03", "포스터 부착 시작"), ("2026-11-15", "주일미사 홍보")]
BOOKING = {"booking-hero": "정보 블록", "booking-sticky": "오른쪽 위 고정 버튼", "booking-bottom": "맨 아래"}
GROUP_NOTE = {"지인": "단원 지인", "신자": "성당 신자", "간접": "가톨릭·대학 연관",
              "무관계": "처음 보는 사람", "전체": "포스터를 본 모든 사람"}


def load_cfg() -> dict:
    """{goatcounter, param, channels: {코드: {name, group}}, groups, stats}. 레포 안이면 config.yaml, 공개 레포면 stats_config.json."""
    if not IN_REPO:
        return json.loads((HERE / "stats_config.json").read_text(encoding="utf-8"))
    sys.path.insert(0, str(HERE))
    import registry as reg
    config = reg.load_config()
    tr = config["tracking"]
    return {"goatcounter": tr["goatcounter"], "param": tr["param"],
            "channels": {c: {"name": ch["name"], "group": ch["group"]} for c, ch in reg.channels(config).items()},
            "groups": list(reg.GROUPS), "stats": config.get("stats") or {},
            "test_submits": {str(k): int(v) for k, v in ((config.get("stats") or {}).get("test_submits") or {}).items()}}


def token() -> str:
    t = os.environ.get("GOATCOUNTER_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    if not t:
        sys.exit(f"토큰이 없습니다: GOATCOUNTER_TOKEN 또는 {TOKEN_FILE}")
    return t


def api_base(cfg) -> str:
    count = cfg.get("goatcounter") or ""
    if not count.endswith("/count"):
        sys.exit("tracking.goatcounter가 …/count 형식이 아닙니다")
    return count[: -len("/count")] + "/api/v0"


def get(base, tok, path, **q):
    url = f"{base}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def code_of(raw: str, known) -> str | None:
    """'ig' → 'IG', 'MS\\u2028🔗' → 'MS', 'NM' → 'DM'. 등록부에 없으면 None."""
    m = re.match(r"[A-Za-z0-9]+", raw or "")
    if not m:
        return None
    c = m.group(0).upper()
    c = ALIASES.get(c, c)
    return c if c in known else None


def typo_code(path: str, param: str, known) -> str | None:
    """'/?ret=MS' 같은 오타 주소 → 'MS'."""
    m = re.fullmatch(r"/\?([A-Za-z_]+)=([^&#]*)", path or "")
    if not m or m.group(1) == param:
        return None
    return code_of(m.group(2), known)


def kst_day_range(day: str):
    """한국 날짜 하루 → API 시각 범위 (GoatCounter 사이트 시간대가 KST라 일별 숫자와 맞는다)."""
    d = datetime.date.fromisoformat(day)
    prev = (d - datetime.timedelta(days=1)).isoformat()
    return f"{prev}T15:00:00Z", f"{day}T14:59:59Z"


def collect(cfg):
    tok, base = token(), api_base(cfg)
    param = cfg["param"]
    chans = cfg["channels"]
    known = set(chans)
    today = datetime.datetime.now(KST).date()
    end = today.isoformat()
    start7 = (today - datetime.timedelta(days=6)).isoformat()
    days, d = [], datetime.date.fromisoformat(START)
    while d <= today:
        days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    idx = {k: i for i, k in enumerate(days)}

    hits = get(base, tok, "/stats/hits", start=START, limit=200)["hits"]
    pages = [h for h in hits if not h["event"]]
    events = {h["path"]: h for h in hits if h["event"]}

    def daily(h):
        return {d["day"]: d["daily"] for d in h["stats"]}

    def last7(h):
        return sum(v for k, v in daily(h).items() if k >= start7)

    # 출처별 날짜 줄: 코드 → [날짜별 방문]. 합계·최근 7일은 이 줄을 더해서 낸다
    rows = {c: [0] * len(days) for c in chans}
    direct, sites, unknown, fixes = [0] * len(days), {}, {}, {}
    normal_ids = []
    for h in pages:
        c = typo_code(h["path"], param, known)
        if c:
            for k, v in daily(h).items():
                if k in idx:
                    rows[c][idx[k]] += v
            fixes[(h["path"], c)] = fixes.get((h["path"], c), 0) + h["count"]
        else:
            normal_ids.append(str(h["path_id"]))

    def refs_of(day):
        s, e = kst_day_range(day)
        for attempt in range(4):
            try:
                return day, get(base, tok, "/stats/toprefs", start=s, end=e, limit=200,
                                include_paths=",".join(normal_ids))["stats"]
            except urllib.error.HTTPError as err:
                if err.code != 429 or attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    if normal_ids:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for day, refs in pool.map(refs_of, days):
                i = idx[day]
                for r in refs:
                    name, n = r.get("name") or "", r["count"]
                    if r.get("ref_scheme") == "c":
                        c = code_of(name, known)
                        if c:
                            rows[c][i] += n
                            up = name.upper()
                            if up != c and ALIASES.get(up) != c:
                                key = (f"?{param}={name}", c)
                                fixes[key] = fixes.get(key, 0) + n
                        else:
                            unknown.setdefault(name, [0] * len(days))[i] += n
                    elif not name:
                        direct[i] += n
                    else:
                        sites.setdefault(name, [0] * len(days))[i] += n

    def row(series):
        return {"total": sum(series), "last7": sum(v for k, v in zip(days, series) if k >= start7), "daily": series}

    def merge(d):
        out = [0] * len(days)
        for s in d.values():
            out = [a + b for a, b in zip(out, s)]
        return out

    page_daily = [daily(h) for h in pages]
    book_daily = [daily(events[p]) for p in BOOKING if p in events]
    series = [{"day": k,
               "visits": sum(x.get(k, 0) for x in page_daily),
               "clicks": sum(x.get(k, 0) for x in book_daily)} for k in days]

    channels = [{"code": c, "name": ch["name"], "group": ch["group"], **row(rows[c])} for c, ch in chans.items()]
    channels.sort(key=lambda x: (-x["total"], -x["last7"]))
    extra = []
    if any(direct):
        extra.append({"label": "코드 없이 들어온 방문", "note": "주소 직접 입력, 북마크, 코드를 뗀 링크", **row(direct)})
    if sites:
        extra.append({"label": "다른 사이트의 코드 없는 링크",
                      "note": " · ".join(f"{k} {sum(v)}" for k, v in sorted(sites.items(), key=lambda kv: -sum(kv[1]))),
                      **row(merge(sites))})
    if unknown:
        extra.append({"label": "알 수 없는 코드", "note": " · ".join(f"{k} {sum(v)}" for k, v in unknown.items()),
                      **row(merge(unknown))})

    groups = []
    for g in cfg["groups"]:
        n = sum(x["total"] for x in channels if x["group"] == g)
        groups.append({"group": g, "note": GROUP_NOTE.get(g, ""), "total": n})

    booking = [{"label": label, "total": events[p]["count"] if p in events else 0,
                "last7": last7(events[p]) if p in events else 0} for p, label in BOOKING.items()]
    # 이벤트 응모: 테스트(더미)로 찍힌 날의 건수를 뺀다 (GoatCounter는 한 건씩 지울 수 없다)
    tests = cfg.get("test_submits") or {}
    sub_daily = daily(events["event-submit"]) if "event-submit" in events else {}
    sub_real = {k: max(0, v - tests.get(k, 0)) for k, v in sub_daily.items()}
    visits = sum(s["visits"] for s in series)
    return {
        "generated": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "start": START, "end": end, "start7": start7,
        "visits": visits, "visits7": sum(s["visits"] for s in series if s["day"] >= start7),
        "clicks": sum(b["total"] for b in booking), "clicks7": sum(b["last7"] for b in booking),
        "submits": sum(sub_real.values()), "submits7": sum(v for k, v in sub_real.items() if k >= start7),
        "channels": channels, "extra": extra, "groups": groups, "booking": booking,
        "fixes": [{"raw": r, "to": c, "count": n} for (r, c), n in fixes.items()], "daily": series,
        "marks": [{"day": k, "label": v} for k, v in MARKS if START <= k <= end],
        "form": booking_form(days, start7),
    }


def booking_form(days, start7):
    """예매 폼 응답 집계 (Apps Script 웹앱). 실패하면 None — 페이지는 예매 섹션 없이 만든다."""
    url = os.environ.get("BOOKING_STATS_URL") or (BOOKING_URL_FILE.read_text().strip() if BOOKING_URL_FILE.exists() else "")
    if not url:
        return None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                b = json.load(r)
            if b.get("ok"):
                break
            print(f"예매 집계 오류: {b.get('error')}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"예매 집계 실패 ({attempt + 1}/3): {type(e).__name__}", file=sys.stderr)   # 주소(비밀값)는 찍지 않는다
            time.sleep(5)
    else:
        return None
    per_day = [b["daily"].get(d, 0) for d in days]
    cum, n = [], 0
    for v in per_day:
        n += v
        cum.append(n)
    sources = []
    for name, by_day in b["source"].items():
        sources.append({"label": name, "total": sum(by_day.values()),
                        "last7": sum(v for k, v in by_day.items() if k >= start7)})
    sources.sort(key=lambda x: (x["label"] == "기타", -x["total"]))
    return {"people": b["people"], "people_dedup": b["people_dedup"],
            "responses": b["responses"], "responses_dedup": b["responses_dedup"],
            "last7": sum(v for k, v in b["daily"].items() if k >= start7),
            "cumulative": cum, "sources": sources,
            # 날짜별 응답 건수 (중복 의심 제외). 웹앱이 옛 버전이면 없다 → 페이지는 인원으로 그린다
            "daily_responses": [b["daily_responses"].get(d, 0) for d in days] if "daily_responses" in b else None, "updated": b.get("updated"), "last_day": b.get("last_day")}


def subset_woff2(name: str, text: str) -> bytes:
    """레포 안에서만: 마루부리 OTF를 쓰인 글자만 남긴 WOFF2로."""
    from fontTools import subset
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]
    font = subset.load_font(str(ROOT / "assets-src" / "fonts" / f"MaruBuri-{name}.otf"), opts)
    s = subset.Subsetter(opts)
    s.populate(text=text)
    s.subset(font)
    buf = io.BytesIO()
    subset.save_font(font, buf, opts)
    return buf.getvalue()


def font_faces(text: str) -> str:
    out = []
    for weight, name in WEIGHTS:
        data = subset_woff2(name, text) if IN_REPO else (HERE / "fonts" / f"MaruBuri-{name}.woff2").read_bytes()
        b64 = base64.b64encode(data).decode()
        out.append(f"@font-face{{font-family:'MaruBuri';font-weight:{weight};font-display:swap;"
                   f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    return "\n".join(out)


def render(data, standalone=False) -> str:
    """standalone: 공개 사이트용 완전한 문서 (아티팩트는 머리말을 플랫폼이 감싼다)."""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    chars = tpl + payload + "0123456789,.·%+−–—()~:/ "
    page = tpl.replace("/*__FONTS__*/", font_faces(chars)).replace("__DATA__", payload)
    if standalone:
        page = ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                '<meta name="robots" content="noindex, nofollow">\n'
                + page.replace("</style>", "</style>\n</head>\n<body>", 1) + "\n</body>\n</html>\n")
    return page


def summary(data) -> str:
    f = data.get("form")
    lines = [f"Éclore 홍보 집계 ({data['start']} ~ {data['end']}, {data['generated']} 기준)",
             f"예매 인원 {f['people']} / 중복 의심 제외 {f['people_dedup']} (응답 {f['responses']}건)" if f else "예매 인원: 연동 안 됨",
             f"방문 {data['visits']} (최근 7일 {data['visits7']}) · 사전예매하기 클릭 {data['clicks']}"
             f" · 이벤트 응모 {data['submits']}", ""]
    for c in data["channels"]:
        lines.append(f"  {c['code']:<3} {c['name']:<24} {c['total']:>5}  (7일 {c['last7']})")
    for e in data["extra"]:
        lines.append(f"  --  {e['label']:<24} {e['total']:>5}  (7일 {e['last7']})")
    if data["fixes"]:
        lines += ["", "합친 오타·변형: " + ", ".join(f"{f['raw']} → {f['to']} {f['count']}" for f in data["fixes"])]
    return "\n".join(lines)


def sync(cfg):
    """공개 통계 레포에 실행에 필요한 것만 올린다 (원본 폰트·config 전체·문서는 올리지 않는다)."""
    st = cfg.get("stats") or {}
    repo = st.get("repo")
    if not repo:
        sys.exit("config.yaml stats.repo가 없습니다")
    clone = ROOT / ".cache" / "stats-repo"
    if not (clone / ".git").exists():
        subprocess.run(["gh", "repo", "clone", repo, str(clone)], check=True)
    git = lambda *a: subprocess.run(["git", *a], cwd=clone, check=True, text=True, capture_output=True)  # noqa: E731
    git("pull", "--ff-only")
    public = {k: cfg[k] for k in ("goatcounter", "param", "channels", "groups", "test_submits")}
    (clone / "stats_config.json").write_text(json.dumps(public, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    shutil.copy2(HERE / "stats.py", clone / "stats.py")
    shutil.copy2(TEMPLATE, clone / "stats_page.html")
    site = HERE / "stats_site"
    (clone / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy2(site / "stats.yml", clone / ".github" / "workflows" / "stats.yml")
    shutil.copy2(site / "README.md", clone / "README.md")
    # 글꼴: 페이지에 나올 수 있는 글자 (템플릿, 이 스크립트의 문구, 채널표, ASCII)
    chars = (TEMPLATE.read_text(encoding="utf-8") + (HERE / "stats.py").read_text(encoding="utf-8")
             + json.dumps(public, ensure_ascii=False) + string.printable + "·%+−–—~→"
             + (ROOT / "promo" / "form-options.txt").read_text(encoding="utf-8") + "기타")   # 예매 폼 유입 경로 답
    (clone / "fonts").mkdir(exist_ok=True)
    for _, name in WEIGHTS:
        (clone / "fonts" / f"MaruBuri-{name}.woff2").write_bytes(subset_woff2(name, chars))
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=clone).returncode == 0:
        print(f"바뀐 것 없음 ({repo})")
        return
    git("commit", "-m", "통계 페이지 갱신 (project_eclore_fiat tools/stats.py --sync)")
    git("push")
    print(f"✓ {repo}에 올림 → Actions가 다시 만들어 배포 (https://{st.get('domain') or repo})")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, help="공개 사이트용: 이 폴더에 index.html (완전한 문서)")
    ap.add_argument("--sync", action="store_true", help="공개 통계 레포에 스크립트·템플릿·채널표·글꼴을 올린다")
    args = ap.parse_args(argv)
    cfg = load_cfg()
    if args.sync:
        if not IN_REPO:
            sys.exit("--sync는 원본 레포에서만")
        sync(cfg)
        return
    data = collect(cfg)
    print(summary(data))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "index.html").write_text(render(data, standalone=True), encoding="utf-8")
        (args.out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
        print(f"\n→ {args.out / 'index.html'}")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stats.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    page = OUT / "eclore-stats.html"
    page.write_text(render(data), encoding="utf-8")
    print(f"\n→ {page.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
