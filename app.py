# app.py — AdHub v4 (API 자동 연동 통합)
import streamlit as st
import pandas as pd
import json, re, base64
from datetime import datetime
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Neicon Report", page_icon="파비콘_0314.png", layout="wide")

# ============ DB 연결 ============
@st.cache_resource
def get_engine():
    url = st.secrets["DATABASE_URL"]
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)

engine = get_engine()

# ============ 공통 쿼리 헬퍼 ============
def q(sql, params=(), fetch=True):
    counter = [0]
    param_dict = {}

    def replacer(_):
        counter[0] += 1
        k = f"p{counter[0]}"
        param_dict[k] = params[counter[0] - 1]
        return f":{k}"

    converted = re.sub(r"\?", replacer, sql)
    converted = converted.replace(
        "INSERT OR IGNORE INTO", "INSERT INTO"
    ).replace(
        "INSERT OR REPLACE INTO", "INSERT INTO"
    )
    converted = re.sub(r"datetime\('now'\)", "now()", converted)

    if "INSERT INTO" in converted and "ON CONFLICT" not in converted and "INSERT OR IGNORE" in sql:
        converted = converted + " ON CONFLICT DO NOTHING"

    with engine.connect() as con:
        result = con.execute(text(converted), param_dict)
        rows = result.fetchall() if fetch else None
        con.commit()
    return rows

def safe_div(a, b):
    return (a / b) if b else 0

# ============ 전환값(ROAS) 여부 판별 헬퍼 ============
def _is_roas_step(label: str) -> bool:
    s = str(label)
    return "전환값" in s or "매출" in s

# ============ 열 그룹 색상 스타일러 ============
_COL_GROUP_COLORS = {
    "cost_group":  "#FFF3E0",
    "eff_group":   "#E8F5E9",
    "conv_group":  "#EDE7F6",
}

def _style_col_groups(df: pd.DataFrame, conv_label: str = "CPA") -> pd.io.formats.style.Styler:
    cost_cols = {"광고비"}
    eff_cols  = {"CTR (%)", "CPM (₩)", "CPC (₩)"}
    conv_cols = {"전환", "CVR (%)", f"{conv_label} (₩)"}
    conv_pattern_cols = {c for c in df.columns
                         if c == "CVR (%)"
                         or c == "구매"
                         or (c.startswith("CVR·") and c.endswith("구매"))
                         or (c.startswith("CPA·") and c.endswith("구매"))
                         or c.startswith("ROAS·")}

    def _color_col(col_name):
        if col_name in cost_cols:
            return f"background-color: {_COL_GROUP_COLORS['cost_group']}"
        if col_name in eff_cols:
            return f"background-color: {_COL_GROUP_COLORS['eff_group']}"
        if col_name in conv_cols or col_name in conv_pattern_cols:
            return f"background-color: {_COL_GROUP_COLORS['conv_group']}"
        return ""

    styles = {col: _color_col(col) for col in df.columns}
    return df.style.apply(
        lambda col: [styles.get(col.name, "")] * len(col), axis=0
    )

# ============ DB 초기화 ============
def init_db():
    with engine.connect() as con:
        con.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            name TEXT,
            role TEXT,
            password TEXT
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS advertisers (
            code TEXT PRIMARY KEY,
            name TEXT,
            total_budget REAL DEFAULT 0,
            show_conversion INTEGER DEFAULT 1,
            show_creative INTEGER DEFAULT 0,
            created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS permissions (
            email TEXT,
            advertiser_code TEXT,
            level TEXT,
            PRIMARY KEY (email, advertiser_code)
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS perf (
            id SERIAL PRIMARY KEY,
            advertiser_code TEXT,
            platform TEXT,
            date TEXT,
            campaign TEXT,
            adgroup TEXT,
            creative TEXT,
            impressions INTEGER,
            clicks INTEGER,
            cost REAL,
            raw_data TEXT,
            upload_log_id INTEGER
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS upload_log (
            id SERIAL PRIMARY KEY,
            email TEXT,
            advertiser_code TEXT,
            platform TEXT,
            file_name TEXT,
            rows INTEGER,
            uploaded_at TEXT,
            upload_mode TEXT,
            deleted_rows INTEGER DEFAULT 0
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS conversion_mapping (
            id SERIAL PRIMARY KEY,
            advertiser_code TEXT,
            platform TEXT,
            campaign TEXT,
            conversion_column TEXT,
            conversion_label TEXT,
            updated_at TEXT,
            UNIQUE(advertiser_code, platform, campaign)
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS funnel_mapping (
            id SERIAL PRIMARY KEY,
            advertiser_code TEXT NOT NULL,
            platform TEXT NOT NULL,
            step_order INTEGER NOT NULL,
            column_name TEXT NOT NULL,
            label TEXT NOT NULL,
            cvr_base TEXT DEFAULT 'clicks'
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS creative_images (
            id SERIAL PRIMARY KEY,
            advertiser_code TEXT NOT NULL,
            platform TEXT NOT NULL,
            creative_name TEXT NOT NULL,
            image_data TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'image/jpeg',
            uploaded_at TEXT,
            uploaded_by TEXT,
            UNIQUE(advertiser_code, platform, creative_name)
        )"""))

        # ── API 연동 신규 테이블 ──────────────────────────────
        con.execute(text("""
        CREATE TABLE IF NOT EXISTS api_credentials (
            id SERIAL PRIMARY KEY,
            advertiser_code TEXT NOT NULL,
            platform TEXT NOT NULL,
            cred_key TEXT NOT NULL,
            cred_value TEXT,
            updated_at TEXT,
            UNIQUE(advertiser_code, platform, cred_key)
        )"""))

        con.execute(text("""
        CREATE TABLE IF NOT EXISTS api_sync_log (
            id SERIAL PRIMARY KEY,
            advertiser_code TEXT,
            platform TEXT,
            date_from TEXT,
            date_to TEXT,
            rows_fetched INTEGER,
            status TEXT,
            message TEXT,
            synced_at TEXT
        )"""))

        con.execute(text("""
        INSERT INTO users (email, name, role, password) VALUES
            ('admin@adhub.com',  '김에이전시', 'AGENCY_ADMIN', '1234'),
            ('manager@scon.com', '박마케터',   'MANAGER',      '1234'),
            ('viewer@scon.com',  '최뷰어',     'VIEWER',       '1234')
        ON CONFLICT (email) DO NOTHING
        """))

        con.execute(text("""
        INSERT INTO advertisers (code, name) VALUES
            ('SCONEC', '스코넥엔터테인먼트'),
            ('GAME_A', '게임사 A'),
            ('GAME_B', '게임사 B')
        ON CONFLICT (code) DO NOTHING
        """))

        con.execute(text("""
        INSERT INTO permissions (email, advertiser_code, level) VALUES
            ('admin@adhub.com',  'SCONEC', 'OWNER'),
            ('admin@adhub.com',  'GAME_A', 'OWNER'),
            ('admin@adhub.com',  'GAME_B', 'OWNER'),
            ('manager@scon.com', 'SCONEC', 'EDITOR'),
            ('viewer@scon.com',  'SCONEC', 'VIEWER')
        ON CONFLICT (email, advertiser_code) DO NOTHING
        """))

        con.commit()

init_db()

# ============================================================
# API 자격증명 헬퍼
# ============================================================
def get_api_creds(adv_code, platform):
    rows = q("""
        SELECT cred_key, cred_value FROM api_credentials
        WHERE advertiser_code=? AND platform=?
    """, (adv_code, platform))
    return {r[0]: r[1] for r in rows} if rows else {}

def save_api_cred(adv_code, platform, key, value):
    q("""
        INSERT INTO api_credentials
            (advertiser_code, platform, cred_key, cred_value, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT (advertiser_code, platform, cred_key) DO UPDATE SET
            cred_value = EXCLUDED.cred_value,
            updated_at = EXCLUDED.updated_at
    """, (adv_code, platform, key, value,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S")), fetch=False)

def delete_api_cred(adv_code, platform):
    q("DELETE FROM api_credentials WHERE advertiser_code=? AND platform=?",
      (adv_code, platform), fetch=False)

# ============================================================
# Meta Marketing API 데이터 수집
# ============================================================
def fetch_meta_insights(ad_account_id, access_token, date_from, date_to,
                        level="adset"):
    """
    Meta Marketing API에서 인사이트 데이터를 수집합니다.
    level: campaign / adset / ad
    반환값: list[dict] — date, campaign, adgroup, creative, impressions, clicks, cost, raw_data
    """
    try:
        from facebook_business.api import FacebookAdsApi
        from facebook_business.adobjects.adaccount import AdAccount
        from facebook_business.adobjects.adsinsights import AdsInsights
    except ImportError:
        raise ImportError(
            "facebook-business 패키지가 설치되지 않았습니다.\n"
            "requirements.txt에 'facebook-business>=17.0.0' 를 추가한 뒤 재배포하세요."
        )

    FacebookAdsApi.init(access_token=access_token)
    account = AdAccount(f"act_{ad_account_id}")

    fields = [
        AdsInsights.Field.date_start,
        AdsInsights.Field.campaign_name,
        AdsInsights.Field.adset_name,
        AdsInsights.Field.ad_name,
        AdsInsights.Field.impressions,
        AdsInsights.Field.clicks,
        AdsInsights.Field.spend,
        AdsInsights.Field.actions,
        AdsInsights.Field.action_values,
    ]

    params = {
        "time_range": {"since": str(date_from), "until": str(date_to)},
        "level": level,
        "time_increment": 1,
        "limit": 500,
    }

    insights = account.get_insights(fields=fields, params=params)
    rows = []

    for item in insights:
        d = dict(item)
        raw_extra = {}

        for action in d.get("actions", []):
            raw_extra[f"action_{action['action_type']}"] = float(action.get("value", 0))
        for av in d.get("action_values", []):
            raw_extra[f"value_{av['action_type']}"] = float(av.get("value", 0))

        spend_usd = float(d.get("spend", 0))
        rows.append({
            "date":        d.get("date_start"),
            "campaign":    d.get("campaign_name", ""),
            "adgroup":     d.get("adset_name", ""),
            "creative":    d.get("ad_name") if level == "ad" else None,
            "impressions": int(d.get("impressions", 0)),
            "clicks":      int(d.get("clicks", 0)),
            "cost":        spend_usd * 1300,   # USD → KRW (1,300원 기준)
            "raw_data":    json.dumps(raw_extra, ensure_ascii=False),
        })

    return rows

# ============================================================
# Google Ads API 데이터 수집
# ============================================================
def fetch_google_ads_data(customer_id, developer_token, client_id,
                          client_secret, refresh_token,
                          date_from, date_to, level="adgroup"):
    """
    Google Ads API에서 성과 데이터를 수집합니다.
    level: campaign / adgroup / ad
    반환값: list[dict]
    """
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        raise ImportError(
            "google-ads 패키지가 설치되지 않았습니다.\n"
            "requirements.txt에 'google-ads>=24.0.0' 를 추가한 뒤 재배포하세요."
        )

    import yaml, tempfile, os

    cid_clean = customer_id.replace("-", "")
    config = {
        "developer_token": developer_token,
        "client_id":       client_id,
        "client_secret":   client_secret,
        "refresh_token":   refresh_token,
        "login_customer_id": cid_clean,
        "use_proto_plus":  True,
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        tmp_path = f.name

    try:
        client     = GoogleAdsClient.load_from_storage(tmp_path)
        ga_service = client.get_service("GoogleAdsService")

        if level == "campaign":
            resource   = "campaign"
            extra_sel  = "campaign.name"
            extra_grp  = "campaign.name"
        elif level == "ad":
            resource   = "ad_group_ad"
            extra_sel  = "campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.name"
            extra_grp  = "campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.name"
        else:
            resource   = "ad_group"
            extra_sel  = "campaign.name, ad_group.name"
            extra_grp  = "campaign.name, ad_group.name"

        query = f"""
            SELECT
                segments.date,
                {extra_sel},
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversions_value,
                metrics.all_conversions,
                metrics.view_through_conversions
            FROM {resource}
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND campaign.status = 'ENABLED'
            ORDER BY segments.date
        """

        response = ga_service.search_stream(customer_id=cid_clean, query=query)
        rows = []

        for batch in response:
            for row in batch.results:
                m   = row.metrics
                seg = row.segments

                raw_extra = {
                    "conversions":              m.conversions,
                    "conversions_value":        m.conversions_value,
                    "all_conversions":          m.all_conversions,
                    "view_through_conversions": m.view_through_conversions,
                }

                adgroup_name  = row.ad_group.name if hasattr(row, "ad_group") else row.campaign.name
                creative_name = None
                if level == "ad":
                    creative_name = row.ad_group_ad.ad.name or str(row.ad_group_ad.ad.id)

                rows.append({
                    "date":        seg.date,
                    "campaign":    row.campaign.name,
                    "adgroup":     adgroup_name,
                    "creative":    creative_name,
                    "impressions": int(m.impressions),
                    "clicks":      int(m.clicks),
                    "cost":        m.cost_micros / 1_000_000,   # micros → KRW
                    "raw_data":    json.dumps(raw_extra, ensure_ascii=False),
                })
    finally:
        os.unlink(tmp_path)

    return rows

# ============================================================
# Google OAuth2 Refresh Token 발급 헬퍼
# ============================================================
def build_google_auth_url(client_id, redirect_uri="urn:ietf:wg:oauth:2.0:oob"):
    """
    사용자가 브라우저에서 열어 Google 계정을 인증할 URL을 생성합니다.
    redirect_uri='urn:ietf:wg:oauth:2.0:oob' : 코드가 화면에 표시됨 (서버 불필요)
    """
    scope = "https://www.googleapis.com/auth/adwords"
    url = (
        "https://accounts.google.com/o/oauth2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
        "&access_type=offline"
        "&prompt=consent"
    )
    return url

def exchange_google_auth_code(client_id, client_secret, auth_code,
                               redirect_uri="urn:ietf:wg:oauth:2.0:oob"):
    """인증 코드를 Refresh Token으로 교환합니다."""
    import requests
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code":          auth_code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    })
    data = resp.json()
    if "refresh_token" not in data:
        raise ValueError(f"Refresh Token 발급 실패: {data}")
    return data["refresh_token"]

# ============================================================
# 수집 데이터 → DB 저장
# ============================================================
def save_api_rows_to_db(rows, adv_code, platform, date_from, date_to,
                        upload_mode="upsert", user_email="api_sync"):
    if not rows:
        return 0, 0

    deleted = 0
    if upload_mode == "upsert":
        dates_in_data = list({r["date"] for r in rows})
        if dates_in_data:
            with engine.connect() as con:
                placeholders = ",".join([f":d{i}" for i in range(len(dates_in_data))])
                result = con.execute(text(
                    f"DELETE FROM perf "
                    f"WHERE advertiser_code=:adv AND platform=:pf "
                    f"AND date IN ({placeholders})"
                ), {"adv": adv_code, "pf": platform,
                    **{f"d{i}": d for i, d in enumerate(dates_in_data)}})
                deleted = result.rowcount
                con.commit()

    with engine.connect() as con:
        result = con.execute(text("""
            INSERT INTO api_sync_log
                (advertiser_code, platform, date_from, date_to,
                 rows_fetched, status, message, synced_at)
            VALUES (:adv, :pf, :df, :dt, :rows, :status, :msg, :ts)
            RETURNING id
        """), {
            "adv":    adv_code, "pf":   platform,
            "df":     str(date_from), "dt": str(date_to),
            "rows":   len(rows),
            "status": "SUCCESS",
            "msg":    f"deleted={deleted}, user={user_email}",
            "ts":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        sync_id = result.fetchone()[0]

        insert_rows = []
        for r in rows:
            insert_rows.append({
                "adv":  adv_code,
                "pf":   platform,
                "date": r["date"],
                "camp": r["campaign"],
                "ag":   r.get("adgroup", r["campaign"]),
                "imp":  int(r.get("impressions", 0)),
                "clk":  int(r.get("clicks", 0)),
                "cost": float(r.get("cost", 0)),
                "raw":  r.get("raw_data", "{}"),
                "uid":  sync_id,
                "cre":  r.get("creative"),
            })

        con.execute(text("""
            INSERT INTO perf
                (advertiser_code, platform, date, campaign, adgroup,
                 impressions, clicks, cost, raw_data, upload_log_id, creative)
            VALUES
                (:adv, :pf, :date, :camp, :ag,
                 :imp, :clk, :cost, :raw, :uid, :cre)
        """), insert_rows)
        con.commit()

    return len(rows), deleted

# ============================================================
# API 연동 탭 UI
# ============================================================
def render_api_sync_tab(adv_code, user_email, can_edit):
    st.markdown("### 🔌 광고 API 자동 연동")
    st.caption(
        "Meta / Google Ads API를 연결하면 파일 없이 날짜 범위만 선택해 "
        "데이터를 자동으로 가져올 수 있습니다."
    )

    if not can_edit:
        st.error("⛔ VIEWER 권한은 API 설정을 변경할 수 없습니다.")
        return

    api_platform = st.radio(
        "매체 선택",
        ["META (Facebook/Instagram)", "GOOGLE Ads"],
        horizontal=True,
        key="api_pf_radio"
    )
    is_meta   = "META" in api_platform
    pf_key    = "FACEBOOK" if is_meta else "GOOGLE"
    creds     = get_api_creds(adv_code, pf_key)

    # ──────────────────────────────────────────────────────────
    # META
    # ──────────────────────────────────────────────────────────
    if is_meta:
        st.subheader("🔑 Meta API 자격증명")

        with st.expander("📖 Access Token 발급 방법", expanded=not bool(creds)):
            st.markdown("""
**방법 1 — System User Token (권장, 만료 없음)**
1. [Meta Business Suite](https://business.facebook.com/) → 설정 → 시스템 사용자 생성
2. 시스템 사용자에게 광고 계정 **광고 관리자** 권한 부여
3. "토큰 생성" 클릭 → `ads_read`, `ads_management` 권한 체크 → 토큰 복사

**방법 2 — Graph API Explorer (테스트용, 60일 만료)**
1. [Graph API Explorer](https://developers.facebook.com/tools/explorer/) 접속
2. 앱 선택 → `ads_read` 권한 추가 → Access Token 복사

**Ad Account ID 확인**
- 광고 관리자 우측 상단 계정명 클릭 → 숫자 ID (예: `act_123456789` 에서 `123456789` 부분)
""")

        # 자격증명 상태 표시
        if creds.get("access_token"):
            st.success(
                f"✅ 자격증명 등록됨  |  "
                f"Ad Account: `{creds.get('ad_account_id','(미설정)')}`"
            )
            if st.button("🗑️ 자격증명 삭제", key="meta_del_cred"):
                delete_api_cred(adv_code, "FACEBOOK")
                st.success("삭제됨")
                st.rerun()
        else:
            st.info("자격증명이 등록되지 않았습니다.")

        with st.form("meta_cred_form"):
            ad_account_id = st.text_input(
                "Ad Account ID (숫자만, act_ 제외)",
                value=creds.get("ad_account_id", ""),
                placeholder="123456789"
            )
            access_token = st.text_input(
                "Access Token",
                value=creds.get("access_token", ""),
                type="password",
                placeholder="EAAxxxxxx..."
            )
            usd_rate = st.number_input(
                "USD → KRW 환율 (비용 단위가 USD인 경우)",
                min_value=1000, max_value=2000,
                value=int(creds.get("usd_rate", 1300)),
                step=10
            )
            if st.form_submit_button("💾 자격증명 저장", type="primary"):
                if not ad_account_id or not access_token:
                    st.error("Ad Account ID와 Access Token을 모두 입력하세요.")
                else:
                    save_api_cred(adv_code, "FACEBOOK", "ad_account_id", ad_account_id.strip())
                    save_api_cred(adv_code, "FACEBOOK", "access_token",  access_token.strip())
                    save_api_cred(adv_code, "FACEBOOK", "usd_rate",      str(usd_rate))
                    st.success("자격증명 저장 완료!")
                    st.rerun()

        # 데이터 수집 UI (자격증명 있을 때만 표시)
        if creds.get("ad_account_id") and creds.get("access_token"):
            st.divider()
            st.subheader("📥 Meta 데이터 수집")

            with st.form("meta_fetch_form"):
                fc1, fc2 = st.columns(2)
                with fc1:
                    m_from = st.date_input(
                        "시작일",
                        value=datetime.now().date().replace(day=1),
                        key="meta_from"
                    )
                with fc2:
                    m_to = st.date_input(
                        "종료일",
                        value=datetime.now().date(),
                        key="meta_to"
                    )

                m_level = st.radio(
                    "수집 단위",
                    ["campaign", "adset", "ad"],
                    horizontal=True,
                    format_func=lambda x: {
                        "campaign": "📁 캠페인",
                        "adset":    "📂 광고세트 (광고그룹)",
                        "ad":       "🎨 광고 소재"
                    }[x],
                    key="meta_level"
                )
                st.caption({
                    "campaign": "캠페인 단위로 집계 — 광고그룹/소재 정보는 포함되지 않음",
                    "adset":    "광고세트(광고그룹) 단위 집계 — 일반적으로 권장",
                    "ad":       "광고 소재 단위 집계 — 소재 분석 탭 활용 시 선택"
                }[m_level])

                m_mode = st.radio(
                    "저장 방식",
                    ["upsert", "append"],
                    horizontal=True,
                    format_func=lambda x: "🔄 기간 덮어쓰기 (권장)" if x == "upsert" else "➕ 추가",
                    key="meta_mode"
                )

                m_rate = int(creds.get("usd_rate", 1300))
                st.caption(f"💱 비용 환율: USD × {m_rate:,} = KRW")

                fetch_btn = st.form_submit_button("🚀 데이터 가져오기", type="primary")

            if fetch_btn:
                if m_from > m_to:
                    st.error("시작일이 종료일보다 늦을 수 없습니다.")
                else:
                    with st.spinner(
                        f"Meta API에서 데이터 수집 중 "
                        f"({m_from} ~ {m_to}, {m_level} 단위)..."
                    ):
                        try:
                            rows = fetch_meta_insights(
                                creds["ad_account_id"],
                                creds["access_token"],
                                m_from, m_to,
                                level=m_level
                            )
                            # 환율 재적용 (저장 함수 내에서 1300 기본 적용되므로 여기서 보정)
                            for r in rows:
                                r["cost"] = r["cost"] / 1300 * m_rate

                            saved, deleted = save_api_rows_to_db(
                                rows, adv_code, "FACEBOOK",
                                m_from, m_to,
                                upload_mode=m_mode,
                                user_email=user_email
                            )
                            st.success(
                                f"🎉 완료!  삭제 {deleted:,}행 → 신규 저장 {saved:,}행"
                            )
                            st.balloons()
                        except Exception as e:
                            st.error(f"❌ 수집 실패: {e}")
                            import traceback; st.code(traceback.format_exc())

    # ──────────────────────────────────────────────────────────
    # GOOGLE
    # ──────────────────────────────────────────────────────────
    else:
        st.subheader("🔑 Google Ads API 자격증명")

        with st.expander("📖 인증 설정 방법 (최초 1회)", expanded=not bool(creds)):
            st.markdown("""
**Step 1 — Google Cloud Console 설정**
1. [console.cloud.google.com](https://console.cloud.google.com) → 새 프로젝트 생성
2. API 및 서비스 → **Google Ads API** 사용 설정
3. 사용자 인증 정보 → OAuth 2.0 클라이언트 ID 생성
   - 유형: **데스크톱 앱**
   - `Client ID`와 `Client Secret` 복사

**Step 2 — Google Ads Developer Token 발급**
1. [Google Ads](https://ads.google.com) → 관리자 계정(MCC) 필요
2. 도구 → API 센터 → Developer Token 확인/신청
3. 기본 토큰으로 테스트 계정 조회 가능 / 실 계정은 Standard Access 신청 필요

**Step 3 — Customer ID 확인**
- Google Ads 우측 상단 계정명 옆 숫자 (예: `123-456-7890`)

**Step 4 — 아래 "Google 계정 연결" 버튼으로 Refresh Token 발급 (1회)**
""")

        if creds.get("refresh_token"):
            st.success(
                f"✅ Google Ads 연결됨  |  "
                f"Customer ID: `{creds.get('customer_id','(미설정)')}`"
            )
            if st.button("🗑️ 자격증명 삭제", key="google_del_cred"):
                delete_api_cred(adv_code, "GOOGLE")
                st.success("삭제됨")
                st.rerun()

        with st.form("google_cred_form"):
            st.markdown("##### 기본 자격증명")
            gc1, gc2 = st.columns(2)
            with gc1:
                customer_id     = st.text_input(
                    "Customer ID",
                    value=creds.get("customer_id", ""),
                    placeholder="123-456-7890"
                )
                client_id       = st.text_input(
                    "OAuth Client ID",
                    value=creds.get("client_id", ""),
                    placeholder="xxx.apps.googleusercontent.com"
                )
            with gc2:
                developer_token = st.text_input(
                    "Developer Token",
                    value=creds.get("developer_token", ""),
                    type="password"
                )
                client_secret   = st.text_input(
                    "OAuth Client Secret",
                    value=creds.get("client_secret", ""),
                    type="password"
                )

            if st.form_submit_button("💾 기본 자격증명 저장", type="primary"):
                if not all([customer_id, developer_token, client_id, client_secret]):
                    st.error("모든 항목을 입력하세요.")
                else:
                    for k, v in [
                        ("customer_id",     customer_id.strip()),
                        ("developer_token", developer_token.strip()),
                        ("client_id",       client_id.strip()),
                        ("client_secret",   client_secret.strip()),
                    ]:
                        save_api_cred(adv_code, "GOOGLE", k, v)
                    st.success("저장 완료! 아래에서 Google 계정을 연결하세요.")
                    st.rerun()

        # Refresh Token 발급 UI (client_id/secret 저장된 후)
        creds_fresh = get_api_creds(adv_code, "GOOGLE")
        if creds_fresh.get("client_id") and creds_fresh.get("client_secret"):
            st.divider()
            st.subheader("🔗 Google 계정 연결 (Refresh Token 발급)")

            if not creds_fresh.get("refresh_token"):
                st.info(
                    "아래 버튼을 눌러 인증 URL을 열고, Google 계정으로 로그인한 뒤 "
                    "표시된 코드를 붙여넣으세요. **최초 1회만 필요합니다.**"
                )
                auth_url = build_google_auth_url(creds_fresh["client_id"])

                col_btn, col_url = st.columns([1, 3])
                with col_btn:
                    st.link_button("🌐 Google 로그인 페이지 열기", auth_url, type="primary")
                with col_url:
                    with st.expander("URL 직접 복사"):
                        st.code(auth_url)

                with st.form("google_token_form"):
                    auth_code = st.text_input(
                        "Google 인증 후 표시된 코드 붙여넣기",
                        placeholder="4/0AX4..."
                    )
                    if st.form_submit_button("✅ Refresh Token 발급", type="primary"):
                        if not auth_code.strip():
                            st.error("인증 코드를 입력하세요.")
                        else:
                            try:
                                refresh_token = exchange_google_auth_code(
                                    creds_fresh["client_id"],
                                    creds_fresh["client_secret"],
                                    auth_code.strip()
                                )
                                save_api_cred(adv_code, "GOOGLE", "refresh_token", refresh_token)
                                st.success(
                                    "🎉 Refresh Token 발급 완료! "
                                    "이후에는 자동으로 인증됩니다."
                                )
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ 발급 실패: {e}")
            else:
                st.success("✅ Refresh Token이 등록되어 있습니다. 자동 수집 준비 완료!")

        # 데이터 수집 UI
        required_keys = ["customer_id","developer_token","client_id","client_secret","refresh_token"]
        if all(creds_fresh.get(k) for k in required_keys):
            st.divider()
            st.subheader("📥 Google Ads 데이터 수집")

            with st.form("google_fetch_form"):
                gc1, gc2 = st.columns(2)
                with gc1:
                    g_from = st.date_input(
                        "시작일",
                        value=datetime.now().date().replace(day=1),
                        key="g_from"
                    )
                with gc2:
                    g_to = st.date_input(
                        "종료일",
                        value=datetime.now().date(),
                        key="g_to"
                    )

                g_level = st.radio(
                    "수집 단위",
                    ["campaign", "adgroup", "ad"],
                    horizontal=True,
                    format_func=lambda x: {
                        "campaign": "📁 캠페인",
                        "adgroup":  "📂 광고그룹",
                        "ad":       "🎨 광고 소재"
                    }[x],
                    key="g_level"
                )
                st.caption({
                    "campaign": "캠페인 단위 집계",
                    "adgroup":  "광고그룹 단위 집계 — 일반적으로 권장",
                    "ad":       "광고 소재 단위 집계 — 소재 분석 탭 활용 시 선택"
                }[g_level])

                g_mode = st.radio(
                    "저장 방식",
                    ["upsert", "append"],
                    horizontal=True,
                    format_func=lambda x: "🔄 기간 덮어쓰기 (권장)" if x == "upsert" else "➕ 추가",
                    key="g_mode"
                )

                g_fetch_btn = st.form_submit_button("🚀 데이터 가져오기", type="primary")

            if g_fetch_btn:
                if g_from > g_to:
                    st.error("시작일이 종료일보다 늦을 수 없습니다.")
                else:
                    with st.spinner(
                        f"Google Ads API에서 데이터 수집 중 "
                        f"({g_from} ~ {g_to}, {g_level} 단위)..."
                    ):
                        try:
                            rows = fetch_google_ads_data(
                                creds_fresh["customer_id"],
                                creds_fresh["developer_token"],
                                creds_fresh["client_id"],
                                creds_fresh["client_secret"],
                                creds_fresh["refresh_token"],
                                g_from, g_to,
                                level=g_level
                            )
                            saved, deleted = save_api_rows_to_db(
                                rows, adv_code, "GOOGLE",
                                g_from, g_to,
                                upload_mode=g_mode,
                                user_email=user_email
                            )
                            st.success(
                                f"🎉 완료!  삭제 {deleted:,}행 → 신규 저장 {saved:,}행"
                            )
                            st.balloons()
                        except Exception as e:
                            st.error(f"❌ 수집 실패: {e}")
                            import traceback; st.code(traceback.format_exc())

    # ── 동기화 이력 공통 ───────────────────────────────────
    st.divider()
    st.subheader("📋 API 동기화 이력")
    sync_logs = pd.read_sql("""
        SELECT
            synced_at   AS "동기화 시각",
            platform    AS "매체",
            date_from   AS "시작일",
            date_to     AS "종료일",
            rows_fetched AS "수집 행수",
            status      AS "상태",
            message     AS "메모"
        FROM api_sync_log
        WHERE advertiser_code = %(code)s
        ORDER BY id DESC
        LIMIT 30
    """, engine, params={"code": adv_code})

    if sync_logs.empty:
        st.info("아직 동기화 이력이 없습니다.")
    else:
        st.dataframe(sync_logs, use_container_width=True, hide_index=True)


# ============ 뷰어 계정 생성 ============
import secrets

def create_viewer_account(adv_code, adv_name):
    email = f"viewer_{adv_code.lower()}@adhub.com"
    temp_pw = secrets.token_urlsafe(6)
    q("INSERT INTO users (email, name, role, password) VALUES (?,?,?,?)",
      (email, f"{adv_name}_뷰어", "VIEWER", temp_pw), fetch=False)
    q("INSERT INTO permissions (email, advertiser_code, level) VALUES (?,?,?)",
      (email, adv_code, "VIEWER"), fetch=False)
    return email, temp_pw

# ============ 소재 이미지 헬퍼 ============
def get_creative_images(adv_code, platform):
    rows = q("""
        SELECT creative_name, image_data, media_type
        FROM creative_images
        WHERE advertiser_code=? AND platform=?
    """, (adv_code, platform))
    return {r[0]: (r[1], r[2]) for r in rows} if rows else {}

def upsert_creative_image(adv_code, platform, creative_name, image_b64, media_type, uploader_email):
    q("""
        INSERT INTO creative_images
            (advertiser_code, platform, creative_name, image_data, media_type, uploaded_at, uploaded_by)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT (advertiser_code, platform, creative_name) DO UPDATE SET
            image_data  = EXCLUDED.image_data,
            media_type  = EXCLUDED.media_type,
            uploaded_at = EXCLUDED.uploaded_at,
            uploaded_by = EXCLUDED.uploaded_by
    """, (adv_code, platform, creative_name, image_b64, media_type,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"), uploader_email), fetch=False)

def delete_creative_image(adv_code, platform, creative_name):
    q("""
        DELETE FROM creative_images
        WHERE advertiser_code=? AND platform=? AND creative_name=?
    """, (adv_code, platform, creative_name), fetch=False)

def get_distinct_creatives(adv_code, platform):
    rows = q("""
        SELECT DISTINCT creative FROM perf
        WHERE advertiser_code=? AND platform=?
          AND creative IS NOT NULL AND creative != '' AND creative != 'None'
        ORDER BY creative
    """, (adv_code, platform))
    return [r[0] for r in rows] if rows else []

# ============ 로그인 ============
def login_view():
    def _get_logo_b64():
        import os
        logo_path = "로고_블랙.png"
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        return None

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        logo_b64 = _get_logo_b64()
        if logo_b64:
            st.markdown(
                f"""
                <div style="display:flex;flex-direction:column;align-items:center;margin-bottom:12px;">
                    <img src="data:image/png;base64,{logo_b64}" width="180" style="margin-bottom:12px;" />
                    <h2 style="text-align:center;margin:0;">Neicon Marketing Report</h2>
                </div>
                """,
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                """
                <div style="display:flex;flex-direction:column;align-items:center;margin-bottom:12px;">
                    <h2 style="text-align:center;margin:0;">Neicon Marketing Report</h2>
                </div>
                """,
                unsafe_allow_html=True
            )
        email = st.text_input("이메일")
        pw    = st.text_input("비밀번호", type="password")
        if st.button("로그인", type="primary", use_container_width=True):
            row = q("SELECT email,name,role FROM users WHERE email=? AND password=?", (email, pw))
            if row:
                r = row[0]
                st.session_state.user = {"email": r[0], "name": r[1], "role": r[2]}
                st.rerun()
            else:
                st.error("로그인 실패")

if "user" not in st.session_state:
    login_view()
    st.stop()

user     = st.session_state.user
is_admin = user["role"] in ("AGENCY_ADMIN", "SUPER_ADMIN")

# ============ 사이드바 ============
my_advs = q("""
    SELECT a.code, a.name, p.level
    FROM permissions p
    JOIN advertisers a ON a.code = p.advertiser_code
    WHERE p.email = ?
    ORDER BY a.name
""", (user["email"],))

with st.sidebar:
    st.markdown(f"**👤 {user['name']}**  \n`{user['role']}`")
    if st.button("로그아웃"):
        del st.session_state.user
        st.rerun()
    st.divider()
    if not my_advs:
        st.warning("접근 가능한 광고주 없음")
        adv_code, my_level, sel_name = None, None, None
    else:
        adv_options = {f"{name} ({code})": (code, level) for code, name, level in my_advs}
        sel = st.selectbox("광고주 선택", list(adv_options.keys()))
        adv_code, my_level = adv_options[sel]
        sel_name = sel
        st.info(f"권한: **{my_level}**")

    menu = ["📈 대시보드"]
    if user["role"] not in ("VIEWER",):
        menu.append("📥 PDF 리포트")
    if my_level in ("OWNER", "EDITOR") or is_admin:
        menu += ["📤 데이터 업로드", "📋 업로드 이력", "🎯 전환지표 설정"]
    if is_admin:
        menu.append("🏢 광고주 관리")
    if is_admin:
        menu.append("👤 계정 관리")
    page = st.radio("메뉴", menu)

# ============ 컬럼 매핑 후보 ============
DATE_CANDS     = ["일", "날짜", "date", "보고 시작", "보고 시작일", "Day"]
CAMP_CANDS     = ["캠페인", "캠페인 이름", "campaign", "campaign name"]
AG_CANDS       = ["광고그룹", "광고 세트 이름", "광고세트 이름", "광고세트", "adgroup", "adset", "ad set name"]
IMP_CANDS      = ["노출수", "노출", "impressions", "impression"]
CLK_CANDS      = ["클릭수", "클릭(전체)", "링크 클릭", "고유 링크 클릭", "클릭", "clicks", "link clicks"]
COST_CANDS     = ["비용", "지출 금액 (KRW)", "지출 금액 (USD)", "지출 금액", "cost", "spend", "amount spent"]
CREATIVE_CANDS = ["소재", "광고소재", "소재명", "광고 이름", "광고 이름(광고)", "ad name", "creative", "creative name", "ad", "광고"]

def guess_column(columns, candidates):
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    for cand in candidates:
        for col in columns:
            if cand.lower() in col.lower():
                return col
    return None

def read_uploaded_file(file):
    name = file.name.lower()
    if name.endswith(("xlsx", "xls")):
        df = pd.read_excel(file)
    else:
        df = pd.read_csv(file)
    df.columns = [str(c).strip() for c in df.columns]
    return df

# ============ 전환 매핑 ============
def get_conversion_mapping(adv_code):
    rows = q("""
        SELECT platform, campaign, conversion_column, conversion_label
        FROM conversion_mapping WHERE advertiser_code=?
    """, (adv_code,))
    return {(p, c): (col, lbl) for p, c, col, lbl in rows}

def resolve_conv(mapping, platform, campaign):
    if (platform, campaign) in mapping:
        return mapping[(platform, campaign)]
    if (platform, "*") in mapping:
        return mapping[(platform, "*")]
    return (None, "CPA")

def compute_metrics(df, mapping):
    if df.empty:
        df["conversions"] = 0
        df["conv_label"]  = "CPA"
        df["conv_column"] = ""
        return df

    def get_conv(row):
        col, _ = resolve_conv(mapping, row["platform"], row["campaign"])
        if not col:
            return 0
        try:
            d = json.loads(row["raw_data"]) if row["raw_data"] else {}
            return float(d.get(col, 0))
        except:
            return 0

    def get_label(row):
        _, lbl = resolve_conv(mapping, row["platform"], row["campaign"])
        return lbl

    def get_col(row):
        col, _ = resolve_conv(mapping, row["platform"], row["campaign"])
        return col or ""

    df = df.copy()
    df["conversions"] = df.apply(get_conv, axis=1)
    df["conv_label"]  = df.apply(get_label, axis=1)
    df["conv_column"] = df.apply(get_col, axis=1)
    return df

# ============ PDF 리포트 ============
import io

def build_pdf_report(adv_code, adv_name, df_all, total_budget, show_conv):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, Image, PageBreak)
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os

    font_name = "Helvetica"
    candidate_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "NanumGothic.ttf"),
        "NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidate_paths:
        try:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont("Korean", path))
                font_name = "Korean"
                break
        except Exception:
            continue

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    styles    = getSampleStyleSheet()
    title_sty = ParagraphStyle("title", parent=styles["Title"], fontName=font_name,
                                fontSize=22, textColor=colors.HexColor("#1F2937"), spaceAfter=20)
    h2_sty    = ParagraphStyle("h2", parent=styles["Heading2"], fontName=font_name,
                                fontSize=14, textColor=colors.HexColor("#1F2937"),
                                spaceAfter=12, spaceBefore=12)
    body_sty  = ParagraphStyle("body", parent=styles["BodyText"], fontName=font_name, fontSize=10)

    story = []

    tot_imp  = int(df_all["impressions"].sum())
    tot_clk  = int(df_all["clicks"].sum())
    tot_cost = float(df_all["cost"].sum())
    tot_conv = float(df_all["conversions"].sum())
    burn     = safe_div(tot_cost, total_budget) * 100 if total_budget else 0
    period   = (f"{df_all['date'].min().strftime('%Y-%m-%d')} ~ "
                f"{df_all['date'].max().strftime('%Y-%m-%d')}")
    labels   = sorted(set(df_all["conv_label"].dropna().unique()))
    conv_label = "/".join(labels) if labels else "CPA"

    story.append(Paragraph(f"📊 {adv_name}", title_sty))
    story.append(Paragraph("광고 성과 리포트", h2_sty))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"기간: {period}", body_sty))
    story.append(Paragraph(f"매체: {', '.join(sorted(df_all['platform'].unique()))}", body_sty))
    story.append(Paragraph(f"생성일: {datetime.now().strftime('%Y-%m-%d %H:%M')}", body_sty))
    story.append(Spacer(1, 18))

    if total_budget:
        story.append(Paragraph("예산 현황", h2_sty))
        budget_data = [
            ["총 예산",   f"₩{total_budget:,.0f}"],
            ["사용 예산", f"₩{tot_cost:,.0f}"],
            ["남은 예산", f"₩{max(total_budget - tot_cost, 0):,.0f}"],
            ["소진율",    f"{burn:.1f}%"],
        ]
        t = Table(budget_data, colWidths=[5*cm, 6*cm])
        t.setStyle(TableStyle([
            ("FONTNAME",   (0, 0), (-1, -1), font_name),
            ("FONTSIZE",   (0, 0), (-1, -1), 10),
            ("BACKGROUND", (0, 0), (0, -1),  colors.HexColor("#F3F4F6")),
            ("TEXTCOLOR",  (1, 3), (1,  3),  colors.HexColor("#ef4444")),
            ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
            ("PADDING",    (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 14))

    story.append(Paragraph("핵심 지표", h2_sty))
    kpi_data = [
        ["지표", "값"],
        ["노출 (Impression)", f"{tot_imp:,}"],
        ["클릭 (Click)",       f"{tot_clk:,}"],
        ["광고비 (Cost)",      f"₩{tot_cost:,.0f}"],
        ["CTR",                f"{safe_div(tot_clk, tot_imp)*100:.2f}%"],
        ["CPM",                f"₩{safe_div(tot_cost, tot_imp)*1000:,.0f}"],
        ["CPC",                f"₩{safe_div(tot_cost, tot_clk):,.0f}"],
    ]
    if show_conv:
        kpi_data.append([f"전환 ({conv_label})", f"{tot_conv:,.0f}"])
        kpi_data.append([conv_label, f"₩{safe_div(tot_cost, tot_conv):,.0f}" if tot_conv else "—"])

    t = Table(kpi_data, colWidths=[6*cm, 6*cm])
    t.setStyle(TableStyle([
        ("FONTNAME",   (0, 0), (-1, -1), font_name),
        ("FONTSIZE",   (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1,  0), colors.HexColor("#4285F4")),
        ("TEXTCOLOR",  (0, 0), (-1,  0), colors.white),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("PADDING",    (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(PageBreak())

    try:
        story.append(Paragraph("일자별 광고비 추이", h2_sty))
        daily = df_all.groupby(["date", "platform"], as_index=False)["cost"].sum()
        fig   = px.line(daily, x="date", y="cost", color="platform", markers=True,
                        color_discrete_map={"GOOGLE": "#4285F4", "FACEBOOK": "#1877F2"})
        fig.update_layout(width=700, height=350, margin=dict(t=20, b=40, l=60, r=20))
        img_bytes = fig.to_image(format="png", scale=2)
        img = Image(io.BytesIO(img_bytes), width=16*cm, height=8*cm)
        story.append(img)
        story.append(Spacer(1, 10))
    except Exception as e:
        story.append(Paragraph(f"(차트 생성 생략: {str(e)[:60]})", body_sty))

    story.append(Paragraph("캠페인별 성과 TOP 15", h2_sty))
    by_camp = df_all.groupby(["platform", "campaign"], as_index=False).agg(
        impressions=("impressions","sum"), clicks=("clicks","sum"),
        cost=("cost","sum"), conversions=("conversions","sum"))
    by_camp = by_camp.sort_values("cost", ascending=False).head(15)

    head = ["매체", "캠페인", "노출", "클릭", "광고비", "CTR"]
    if show_conv:
        head.append(conv_label)
    table_data = [head]
    for _, row in by_camp.iterrows():
        camp = row["campaign"][:30] + ("…" if len(row["campaign"]) > 30 else "")
        line = [row["platform"], camp,
                f"{int(row['impressions']):,}", f"{int(row['clicks']):,}",
                f"₩{int(row['cost']):,}",
                f"{safe_div(row['clicks'], row['impressions'])*100:.2f}%"]
        if show_conv:
            line.append(f"₩{safe_div(row['cost'], row['conversions']):,.0f}"
                        if row["conversions"] else "—")
        table_data.append(line)

    t = Table(table_data, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME",   (0, 0), (-1, -1), font_name),
        ("FONTSIZE",   (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1,  0), colors.HexColor("#4285F4")),
        ("TEXTCOLOR",  (0, 0), (-1,  0), colors.white),
        ("GRID",       (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
        ("ALIGN",      (2, 1), (-1, -1), "RIGHT"),
        ("PADDING",    (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    doc.build(story)
    output.seek(0)
    return output.getvalue()

# ============ 차트 / KPI 헬퍼 ============
def chart_daily_metric(df, conv_label, key_prefix=""):
    if df.empty:
        return
    metric_choice = st.radio(
        "지표 선택", ["CTR (%)","CPM (₩)","CPC (₩)",f"{conv_label} (₩)"],
        horizontal=True, key=f"{key_prefix}_metric")
    daily = df.groupby(["date", "platform"], as_index=False).agg(
        impressions=("impressions","sum"), clicks=("clicks","sum"),
        cost=("cost","sum"), conversions=("conversions","sum"))
    daily["CTR"] = daily.apply(lambda r: safe_div(r.clicks, r.impressions)*100, axis=1)
    daily["CPM"] = daily.apply(lambda r: safe_div(r.cost, r.impressions)*1000, axis=1)
    daily["CPC"] = daily.apply(lambda r: safe_div(r.cost, r.clicks), axis=1)
    daily["CPA"] = daily.apply(lambda r: safe_div(r.cost, r.conversions), axis=1)
    mmap  = {"CTR (%)":"CTR","CPM (₩)":"CPM","CPC (₩)":"CPC",f"{conv_label} (₩)":"CPA"}
    y_col = mmap[metric_choice]
    fig = px.line(daily, x="date", y=y_col, color="platform", markers=True,
                  title=f"일자별 {metric_choice} 추이",
                  color_discrete_map={"GOOGLE":"#4285F4","FACEBOOK":"#1877F2"})
    fig.update_layout(height=380, hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_daily_chart")

def chart_cost_donut(df, title="매체별 광고비 비중"):
    by_pf = df.groupby("platform", as_index=False)["cost"].sum()
    if by_pf.empty or by_pf["cost"].sum() == 0:
        st.info("광고비 데이터 없음")
        return
    fig = px.pie(by_pf, names="platform", values="cost", hole=0.5, title=title,
                 color="platform",
                 color_discrete_map={"GOOGLE":"#4285F4","FACEBOOK":"#1877F2"})
    fig.update_traces(textposition="inside", textinfo="percent+label")
    fig.update_layout(height=350)
    st.plotly_chart(fig, use_container_width=True, key=f"cost_donut_{title}")

def chart_campaign_bar(df, metric="cost", title="캠페인별 광고비 TOP 10"):
    by_camp = df.groupby(["campaign","platform"], as_index=False).agg(
        cost=("cost","sum"), clicks=("clicks","sum"),
        impressions=("impressions","sum"), conversions=("conversions","sum"))
    by_camp = by_camp.sort_values(metric, ascending=False).head(10)
    if by_camp.empty:
        return
    fig = px.bar(by_camp, x=metric, y="campaign", color="platform", orientation="h",
                 title=title,
                 color_discrete_map={"GOOGLE":"#4285F4","FACEBOOK":"#1877F2"})
    fig.update_layout(height=400, yaxis={"categoryorder":"total ascending"})
    st.plotly_chart(fig, use_container_width=True, key=f"camp_bar_{title}")

def render_budget_donut(spent, total, height=240):
    remaining = max(total - spent, 0)
    burn = safe_div(spent, total) * 100
    fig  = go.Figure(data=[go.Pie(
        labels=["소진","잔여"],
        values=[spent, remaining],
        hole=0.62,
        marker=dict(colors=["#ef4444","#e5e7eb"], line=dict(color="white", width=2)),
        textinfo="none", sort=False, direction="clockwise",
    )])
    fig.update_layout(
        height=height,
        margin=dict(t=10, b=10, l=10, r=10),
        showlegend=True,
        legend=dict(orientation="v", x=1.02, y=0.5, font=dict(size=11)),
        annotations=[dict(
            text=f"<b style='font-size:26px'>{burn:.1f}%</b><br>"
                 f"<span style='font-size:11px;color:#666'>소진율</span>",
            x=0.5, y=0.5, showarrow=False
        )]
    )
    return fig

def render_kpi(df, total_budget=0, show_conversion=True, key_suffix=""):
    tot_imp  = int(df["impressions"].sum())
    tot_clk  = int(df["clicks"].sum())
    tot_cost = float(df["cost"].sum())
    tot_conv = float(df["conversions"].sum())
    ctr = safe_div(tot_clk, tot_imp)*100
    cpm = safe_div(tot_cost, tot_imp)*1000
    cpc = safe_div(tot_cost, tot_clk)
    cpa = safe_div(tot_cost, tot_conv)
    labels     = sorted(set(df["conv_label"].dropna().unique()))
    conv_label = "/".join(labels) if labels else "CPA"

    if total_budget > 0:
        col_left, col_right = st.columns(2)
        with col_left:
            st.markdown("#### 예산 현황")
            col_donut, col_budget = st.columns([1, 1])
            with col_donut:
                st.plotly_chart(
                    render_budget_donut(tot_cost, total_budget),
                    use_container_width=True,
                    key=f"budget_donut_{key_suffix}_{tot_cost}_{total_budget}")
            with col_budget:
                st.metric("총 예산",    f"₩{total_budget:,.0f}")
                st.metric("소진 광고비", f"₩{tot_cost:,.0f}")
                st.metric("잔여 예산",  f"₩{max(total_budget - tot_cost, 0):,.0f}")
        with col_right:
            st.markdown("#### 주요 지표")
            if show_conversion:
                r1 = st.columns(2)
                r1[0].metric("노출", f"{tot_imp:,}")
                r1[1].metric("클릭", f"{tot_clk:,}")
                r2 = st.columns(2)
                r2[0].metric(f"전환 ({conv_label})", f"{tot_conv:,.0f}")
                r2[1].metric("CTR", f"{ctr:.2f}%")
                r3 = st.columns(2)
                r3[0].metric("CPM", f"₩{cpm:,.0f}")
                r3[1].metric("CPC", f"₩{cpc:,.0f}")
                if tot_conv:
                    st.metric(conv_label, f"₩{cpa:,.0f}")
            else:
                r1 = st.columns(2)
                r1[0].metric("노출", f"{tot_imp:,}")
                r1[1].metric("클릭", f"{tot_clk:,}")
                r2 = st.columns(3)
                r2[0].metric("CTR", f"{ctr:.2f}%")
                r2[1].metric("CPM", f"₩{cpm:,.0f}")
                r2[2].metric("CPC", f"₩{cpc:,.0f}")
    else:
        if show_conversion:
            c = st.columns(4)
            c[0].metric("광고비", f"₩{tot_cost:,.0f}")
            c[1].metric("노출",   f"{tot_imp:,}")
            c[2].metric("클릭",   f"{tot_clk:,}")
            c[3].metric(f"전환 ({conv_label})", f"{tot_conv:,.0f}")
            c2 = st.columns(4)
            c2[0].metric("CTR", f"{ctr:.2f}%")
            c2[1].metric("CPM", f"₩{cpm:,.0f}")
            c2[2].metric("CPC", f"₩{cpc:,.0f}")
            c2[3].metric(conv_label, f"₩{cpa:,.0f}" if tot_conv else "—")
        else:
            c = st.columns(3)
            c[0].metric("광고비", f"₩{tot_cost:,.0f}")
            c[1].metric("노출",   f"{tot_imp:,}")
            c[2].metric("클릭",   f"{tot_clk:,}")
            c2 = st.columns(3)
            c2[0].metric("CTR", f"{ctr:.2f}%")
            c2[1].metric("CPM", f"₩{cpm:,.0f}")
            c2[2].metric("CPC", f"₩{cpc:,.0f}")

    return conv_label

def _add_metric_cols(g, conv_label, show_conversion=True):
    g = g.copy()
    g["CTR (%)"] = g.apply(lambda r: round(safe_div(r["clicks"], r["impressions"])*100, 2), axis=1)
    g["CPM (₩)"] = g.apply(lambda r: round(safe_div(r["cost"],   r["impressions"])*1000), axis=1)
    g["CPC (₩)"] = g.apply(lambda r: round(safe_div(r["cost"],   r["clicks"])),            axis=1)
    if show_conversion:
        g[f"{conv_label} (₩)"] = g.apply(
            lambda r: round(safe_div(r["cost"], r["conversions"])) if r["conversions"] else 0, axis=1)
    g["광고비"] = g["cost"].astype(int)
    g["노출"]   = g["impressions"]
    g["클릭"]   = g["clicks"]
    if show_conversion:
        g["전환"] = g["conversions"].astype(int)
    return g

# ============ 퍼널 관련 헬퍼 ============
def get_raw_data_columns(adv_code, platform):
    rows = q("""
        SELECT raw_data FROM perf
        WHERE advertiser_code=? AND platform=? AND raw_data IS NOT NULL
        ORDER BY id DESC LIMIT 100
    """, (adv_code, platform))
    cols = set()
    for r in rows:
        try:
            d = json.loads(r[0])
            for k, v in d.items():
                try:
                    float(v)
                    cols.add(k)
                except:
                    pass
        except:
            pass
    return sorted(cols)

def get_funnel_steps(adv_code, platform):
    rows = q("""
        SELECT step_order, column_name, label, COALESCE(cvr_base,'clicks')
        FROM funnel_mapping WHERE advertiser_code=? AND platform=?
        ORDER BY step_order
    """, (adv_code, platform))
    return [{"order": r[0], "column": r[1], "label": r[2], "cvr_base": r[3]} for r in rows]

def save_funnel_steps(adv_code, platform, steps):
    q("DELETE FROM funnel_mapping WHERE advertiser_code=? AND platform=?",
      (adv_code, platform), fetch=False)
    for i, s in enumerate(steps, 1):
        q("""
            INSERT INTO funnel_mapping
                (advertiser_code, platform, step_order, column_name, label, cvr_base)
            VALUES (?,?,?,?,?,?)
        """, (adv_code, platform, i, s["column"], s["label"], s["cvr_base"]), fetch=False)

def _add_funnel_cols_to_df(df, funnel_steps):
    if not funnel_steps or df.empty:
        return df
    df = df.copy()
    parsed = df["raw_data"].fillna("{}").apply(
        lambda x: json.loads(x) if isinstance(x, str) and x else {})
    for step in sorted(funnel_steps, key=lambda x: x["order"]):
        col = step["column"]
        df[f"_funnel_{step['order']}"] = parsed.apply(
            lambda d, c=col: float(d.get(c, 0) or 0))
    return df

def _build_funnel_agg_cols(g, funnel_steps):
    result = g.copy()
    for step in sorted(funnel_steps, key=lambda x: x["order"]):
        k = f"_funnel_{step['order']}"
        if k in result.columns:
            if _is_roas_step(step["label"]):
                result[step["label"]] = result[k].astype(float)
            else:
                result[step["label"]] = result[k].astype(int)
    return result

def _add_funnel_rate_cols(g, funnel_steps):
    sorted_steps = sorted(funnel_steps, key=lambda x: x["order"])
    for i, step in enumerate(sorted_steps):
        label    = step["label"]
        cvr_base = step.get("cvr_base", "clicks")
        cnt_col  = label

        if _is_roas_step(label):
            g[f"ROAS·{label}"] = g.apply(
                lambda r, cl=cnt_col:
                    f"{safe_div(r[cl], r['cost']) * 100:.1f}%" if r["cost"] > 0 else "—",
                axis=1)
        else:
            if cvr_base == "previous" and i > 0:
                prev_label = sorted_steps[i-1]["label"]
                g[f"CVR·{label}"] = g.apply(
                    lambda r, cl=cnt_col, pl=prev_label:
                        f"{safe_div(r[cl], r[pl])*100:.1f}%" if r[pl] > 0 else "—", axis=1)
            else:
                g[f"CVR·{label}"] = g.apply(
                    lambda r, cl=cnt_col:
                        f"{safe_div(r[cl], r['clicks'])*100:.1f}%" if r["clicks"] > 0 else "—", axis=1)
            g[f"CPA·{label}"] = g.apply(
                lambda r, cl=cnt_col:
                    f"₩{int(safe_div(r['cost'], r[cl])):,}" if r[cl] > 0 else "—", axis=1)
    return g

def _get_funnel_extra_cols(funnel_steps):
    extra = []
    if not funnel_steps:
        return extra
    for step in sorted(funnel_steps, key=lambda x: x["order"]):
        label = step["label"]
        if _is_roas_step(label):
            extra.append(f"ROAS·{label}")
        else:
            extra.append(f"CVR·{label}")
            extra.append(f"CPA·{label}")
    return extra

def _make_funnel_total_vals(total_row, funnel_steps, df_f, tot_clk, tot_cost):
    if not funnel_steps:
        return total_row
    sorted_steps = sorted(funnel_steps, key=lambda x: x["order"])
    for i, step in enumerate(sorted_steps):
        label = step["label"]
        k     = f"_funnel_{step['order']}"
        tot_s = float(df_f[k].sum()) if k in df_f.columns else 0

        if _is_roas_step(label):
            total_row[label]             = f"₩{int(tot_s):,}"
            total_row[f"ROAS·{label}"]   = (
                f"{safe_div(tot_s, tot_cost) * 100:.1f}%" if tot_cost > 0 else "—")
        else:
            total_row[label] = f"{int(tot_s):,}"
            cvr_base = step.get("cvr_base", "clicks")
            if cvr_base == "previous" and i > 0:
                prev_k    = f"_funnel_{sorted_steps[i-1]['order']}"
                prev_tot  = float(df_f[prev_k].sum()) if prev_k in df_f.columns else 0
                total_row[f"CVR·{label}"] = (
                    f"{safe_div(tot_s, prev_tot)*100:.1f}%" if prev_tot > 0 else "—")
            else:
                total_row[f"CVR·{label}"] = (
                    f"{safe_div(tot_s, tot_clk)*100:.1f}%" if tot_clk else "—")
            total_row[f"CPA·{label}"] = (
                f"₩{int(safe_div(tot_cost, tot_s)):,}" if tot_s else "—")
    return total_row

def render_campaign_table(df, conv_label, key, show_conversion=True, funnel_steps=None):
    unit        = st.radio("집계 단위", ["캠페인 합계","일자별"], horizontal=True, key=f"{key}_unit")
    base_cols   = ["노출","클릭","광고비"]
    metric_cols = ["CTR (%)","CPM (₩)","CPC (₩)"]
    if show_conversion:
        base_cols.append("전환")
        metric_cols.append(f"{conv_label} (₩)")

    df_f          = _add_funnel_cols_to_df(df, funnel_steps) if funnel_steps else df
    funnel_labels = [s["label"] for s in sorted(funnel_steps, key=lambda x: x["order"])] if funnel_steps else []
    extra_cols    = _get_funnel_extra_cols(funnel_steps)

    if unit == "캠페인 합계":
        agg_dict = dict(impressions=("impressions","sum"), clicks=("clicks","sum"),
                        cost=("cost","sum"), conversions=("conversions","sum"))
        if funnel_steps:
            for step in funnel_steps:
                k = f"_funnel_{step['order']}"
                if k in df_f.columns:
                    agg_dict[k] = (k, "sum")
        g = df_f.groupby("campaign", as_index=False).agg(**agg_dict)
        g = _add_metric_cols(g, conv_label, show_conversion)
        if funnel_steps:
            g = _build_funnel_agg_cols(g, funnel_steps)
            g = _add_funnel_rate_cols(g, funnel_steps)
        show_cols = ["campaign"] + base_cols + metric_cols + funnel_labels + extra_cols
        show_cols = [c for c in show_cols if c in g.columns]
        show = g[show_cols].rename(columns={"campaign":"캠페인"}).sort_values("광고비", ascending=False)
    else:
        agg_dict = dict(impressions=("impressions","sum"), clicks=("clicks","sum"),
                        cost=("cost","sum"), conversions=("conversions","sum"))
        if funnel_steps:
            for step in funnel_steps:
                k = f"_funnel_{step['order']}"
                if k in df_f.columns:
                    agg_dict[k] = (k, "sum")
        g = df_f.groupby(["date","campaign"], as_index=False).agg(**agg_dict)
        g = _add_metric_cols(g, conv_label, show_conversion)
        if funnel_steps:
            g = _build_funnel_agg_cols(g, funnel_steps)
            g = _add_funnel_rate_cols(g, funnel_steps)
        g["일자"] = pd.to_datetime(g["date"]).dt.strftime("%Y-%m-%d")
        show_cols = ["일자","campaign"] + base_cols + metric_cols + funnel_labels + extra_cols
        show_cols = [c for c in show_cols if c in g.columns]
        show = g[show_cols].rename(columns={"campaign":"캠페인"}).sort_values(
            ["일자","광고비"], ascending=[True, False])

    show = show.copy()

    raw_g = df_f.groupby("campaign" if unit == "캠페인 합계" else ["date","campaign"],
                          as_index=False).agg(
        impressions=("impressions","sum"), clicks=("clicks","sum"),
        cost=("cost","sum"), conversions=("conversions","sum"))
    tot_imp  = int(raw_g["impressions"].sum())
    tot_clk  = int(raw_g["clicks"].sum())
    tot_cost = float(raw_g["cost"].sum())
    tot_conv = float(raw_g["conversions"].sum())

    total_row = {"캠페인": "🔢 Total"}
    if unit == "일자별":
        total_row["일자"] = ""
    total_row["노출"]    = f"{tot_imp:,}"
    total_row["클릭"]    = f"{tot_clk:,}"
    total_row["광고비"]  = f"₩{int(tot_cost):,}"
    total_row["CTR (%)"] = f"{safe_div(tot_clk, tot_imp)*100:.2f}"
    total_row["CPM (₩)"] = f"₩{int(safe_div(tot_cost, tot_imp)*1000):,}"
    total_row["CPC (₩)"] = f"₩{int(safe_div(tot_cost, tot_clk)):,}"
    if show_conversion:
        total_row["전환"] = f"{int(tot_conv):,}"
        total_row[f"{conv_label} (₩)"] = f"₩{int(safe_div(tot_cost, tot_conv)):,}" if tot_conv else "—"

    total_row = _make_funnel_total_vals(total_row, funnel_steps, df_f, tot_clk, tot_cost)
    total_df  = pd.DataFrame([total_row])
    total_df  = total_df[[c for c in show.columns if c in total_df.columns]]

    col_config = {
        "노출":     st.column_config.NumberColumn("노출",     format="%,d"),
        "클릭":     st.column_config.NumberColumn("클릭",     format="%,d"),
        "광고비":   st.column_config.NumberColumn("광고비",   format="₩%,d"),
        "CTR (%)": st.column_config.NumberColumn("CTR (%)", format="%.2f%%"),
        "CPM (₩)": st.column_config.NumberColumn("CPM (₩)", format="₩%,d"),
        "CPC (₩)": st.column_config.NumberColumn("CPC (₩)", format="₩%,d"),
    }
    if show_conversion:
        col_config["전환"] = st.column_config.NumberColumn("전환", format="%,d")
        col_config[f"{conv_label} (₩)"] = st.column_config.NumberColumn(
            f"{conv_label} (₩)", format="₩%,d")
    for lbl in funnel_labels:
        if _is_roas_step(lbl):
            col_config[lbl] = st.column_config.NumberColumn(lbl, format="₩%,.0f")
        else:
            col_config[lbl] = st.column_config.NumberColumn(lbl, format="%,d")

    st.dataframe(total_df, use_container_width=True, hide_index=True)
    st.dataframe(
        _style_col_groups(show, conv_label),
        use_container_width=True, hide_index=True, column_config=col_config
    )

def render_adgroup_table(df, conv_label, key, show_conversion=True, funnel_steps=None):
    unit        = st.radio("집계 단위", ["광고그룹 합계","일자별"], horizontal=True, key=f"{key}_unit")
    base_cols   = ["노출","클릭","광고비"]
    metric_cols = ["CTR (%)","CPM (₩)","CPC (₩)"]
    if show_conversion:
        base_cols.append("전환")
        metric_cols.append(f"{conv_label} (₩)")

    funnel_labels = [s["label"] for s in sorted(funnel_steps, key=lambda x: x["order"])] if funnel_steps else []
    extra_cols    = _get_funnel_extra_cols(funnel_steps)
    df_f          = _add_funnel_cols_to_df(df, funnel_steps) if funnel_steps else df.copy()

    def _make_agg_dict(df_src):
        agg_dict = dict(impressions=("impressions","sum"), clicks=("clicks","sum"),
                        cost=("cost","sum"), conversions=("conversions","sum"))
        if funnel_steps:
            for step in funnel_steps:
                k = f"_funnel_{step['order']}"
                if k in df_src.columns:
                    agg_dict[k] = (k, "sum")
        return agg_dict

    def _format_row_df(g_raw, id_cols):
        g = _add_metric_cols(g_raw, conv_label, show_conversion)
        if funnel_steps:
            g = _build_funnel_agg_cols(g, funnel_steps)
            g = _add_funnel_rate_cols(g, funnel_steps)
        show_cols = id_cols + base_cols + metric_cols + funnel_labels + extra_cols
        show_cols = [c for c in show_cols if c in g.columns]
        return g[show_cols].copy()

    def _make_total_row(df_src, id_val_dict):
        tot_imp  = int(df_src["impressions"].sum())
        tot_clk  = int(df_src["clicks"].sum())
        tot_cost = float(df_src["cost"].sum())
        tot_conv = float(df_src["conversions"].sum()) if "conversions" in df_src.columns else 0
        row = dict(id_val_dict)
        row["노출"]    = f"{tot_imp:,}"
        row["클릭"]    = f"{tot_clk:,}"
        row["광고비"]  = f"₩{int(tot_cost):,}"
        row["CTR (%)"] = f"{safe_div(tot_clk, tot_imp)*100:.2f}"
        row["CPM (₩)"] = f"₩{int(safe_div(tot_cost, tot_imp)*1000):,}"
        row["CPC (₩)"] = f"₩{int(safe_div(tot_cost, tot_clk)):,}"
        if show_conversion:
            row["전환"] = f"{int(tot_conv):,}"
            row[f"{conv_label} (₩)"] = f"₩{int(safe_div(tot_cost, tot_conv)):,}" if tot_conv else "—"
        row = _make_funnel_total_vals(row, funnel_steps, df_f, tot_clk, tot_cost)
        return row

    ag_col_config = {
        "노출":     st.column_config.NumberColumn("노출",     format="%,d"),
        "클릭":     st.column_config.NumberColumn("클릭",     format="%,d"),
        "광고비":   st.column_config.NumberColumn("광고비",   format="₩%,d"),
        "CTR (%)": st.column_config.NumberColumn("CTR (%)", format="%.2f%%"),
        "CPM (₩)": st.column_config.NumberColumn("CPM (₩)", format="₩%,d"),
        "CPC (₩)": st.column_config.NumberColumn("CPC (₩)", format="₩%,d"),
    }
    if show_conversion:
        ag_col_config["전환"] = st.column_config.NumberColumn("전환", format="%,d")
        ag_col_config[f"{conv_label} (₩)"] = st.column_config.NumberColumn(
            f"{conv_label} (₩)", format="₩%,d")
    for lbl in funnel_labels:
        if _is_roas_step(lbl):
            ag_col_config[lbl] = st.column_config.NumberColumn(lbl, format="₩%,.0f")
        else:
            ag_col_config[lbl] = st.column_config.NumberColumn(lbl, format="%,d")

    if unit == "광고그룹 합계":
        agg_dict  = _make_agg_dict(df_f)
        g         = df_f.groupby("adgroup", as_index=False).agg(**agg_dict)
        out       = _format_row_df(g, ["adgroup"])
        out       = out.rename(columns={"adgroup":"광고그룹"}).sort_values("광고비", ascending=False)
        total_row = _make_total_row(df_f, {"광고그룹": "🔢 Total"})
        total_df  = pd.DataFrame([total_row])
        total_df  = total_df[[c for c in out.columns if c in total_df.columns]]
        st.dataframe(total_df, use_container_width=True, hide_index=True)
        st.dataframe(
            _style_col_groups(out, conv_label),
            use_container_width=True, hide_index=True, column_config=ag_col_config
        )
    else:
        total_row = _make_total_row(df_f, {"광고그룹": "🔢 Total", "일자": ""})
        total_df  = pd.DataFrame([total_row])

        agg_dict = _make_agg_dict(df_f)
        g_ag     = df_f.groupby("adgroup", as_index=False).agg(**agg_dict)
        g_ag     = g_ag.sort_values("cost", ascending=False)

        all_header_cols = ["광고그룹","일자"] + base_cols + metric_cols + funnel_labels + extra_cols
        st.dataframe(total_df[[c for c in all_header_cols if c in total_df.columns]],
                     use_container_width=True, hide_index=True)

        for _, ag_row in g_ag.iterrows():
            ag_name = ag_row["adgroup"]
            ag_cost = float(ag_row["cost"])
            ag_imp  = int(ag_row["impressions"])
            df_ag   = df_f[df_f["adgroup"] == ag_name]

            with st.expander(
                f"📂 {ag_name}  (광고비 ₩{ag_cost:,.0f} · 노출 {ag_imp:,})",
                expanded=False
            ):
                ag_total_row = _make_total_row(
                    df_ag, {"광고그룹": f"↳ {ag_name} 합계", "일자": ""})
                ag_total_df = pd.DataFrame([ag_total_row])

                agg_dict2 = _make_agg_dict(df_ag)
                g_date    = df_ag.groupby("date", as_index=False).agg(**agg_dict2)
                g_date["일자"]    = pd.to_datetime(g_date["date"]).dt.strftime("%Y-%m-%d")
                g_date["광고그룹"] = ag_name
                out_date  = _format_row_df(g_date, ["일자","광고그룹"])
                out_date  = out_date.drop(columns=["광고그룹"], errors="ignore")
                out_date  = out_date.sort_values("일자", ascending=True)

                show_cols_exp   = ["일자"] + base_cols + metric_cols + funnel_labels + extra_cols
                show_cols_exp   = [c for c in show_cols_exp if c in out_date.columns]
                all_cols_total  = ["광고그룹","일자"] + base_cols + metric_cols + funnel_labels + extra_cols
                ag_total_df     = ag_total_df[[c for c in all_cols_total if c in ag_total_df.columns]]

                st.dataframe(ag_total_df, use_container_width=True, hide_index=True)
                st.dataframe(
                    _style_col_groups(out_date[show_cols_exp], conv_label),
                    use_container_width=True, hide_index=True, column_config=ag_col_config
                )

# ============================================================
# 소재 이미지 갤러리
# ============================================================
def render_creative_image_gallery(creative_images_dict, creative_cost_order, key_prefix=""):
    creatives_with_img = [c for c in creative_cost_order if c in creative_images_dict]
    creatives_no_img   = [c for c in creative_cost_order if c not in creative_images_dict]

    if not creatives_with_img:
        st.info("💡 등록된 소재 이미지가 없습니다.")
        return

    st.subheader("🖼️ 소재 이미지 갤러리")
    st.caption(
        f"이미지 등록 {len(creatives_with_img)}개 · "
        f"미등록 {len(creatives_no_img)}개  |  광고비 내림차순 정렬"
    )

    cols_per_row = st.select_slider(
        "한 행에 표시할 소재 수", options=[2,3,4,5,6], value=4,
        key=f"{key_prefix}_gallery_cols")

    card_style = """
        <style>
        .cre-card{border:1px solid #e5e7eb;border-radius:10px;padding:8px;
            background:#ffffff;box-shadow:0 1px 4px rgba(0,0,0,.07);text-align:center;height:100%}
        .cre-card img{width:100%;border-radius:6px;object-fit:contain;max-height:180px}
        .cre-card .cre-name{font-size:11px;color:#374151;margin-top:6px;
            word-break:break-all;font-weight:600;line-height:1.4}
        </style>"""
    st.markdown(card_style, unsafe_allow_html=True)

    for i in range(0, len(creatives_with_img), cols_per_row):
        chunk = creatives_with_img[i: i + cols_per_row]
        cols  = st.columns(cols_per_row)
        for j, cre_name in enumerate(chunk):
            b64_data, media_type = creative_images_dict[cre_name]
            img_src    = f"data:{media_type};base64,{b64_data}"
            short_name = cre_name if len(cre_name) <= 30 else cre_name[:28] + "…"
            with cols[j]:
                st.markdown(
                    f'<div class="cre-card"><img src="{img_src}" alt="{short_name}"/>'
                    f'<div class="cre-name" title="{cre_name}">{short_name}</div></div>',
                    unsafe_allow_html=True)

    if creatives_no_img:
        with st.expander(f"⚠️ 이미지 미등록 소재 {len(creatives_no_img)}개"):
            for nm in creatives_no_img:
                st.markdown(f"- `{nm}`")

# ============================================================
# 광고 소재 탭
# ============================================================
def render_creative_tab(df_pf, platform, key_prefix, show_conv=True,
                        funnel_steps=None, adv_code=None):
    df_cre = df_pf[
        df_pf["creative"].notna() &
        (df_pf["creative"] != "") &
        (df_pf["creative"] != "None")
    ]
    if df_cre.empty:
        st.info(f"💡 {platform} 매체에 광고 소재 데이터가 없습니다.")
        return

    st.caption("🎨 광고 이미지·영상·텍스트별 성과를 분석합니다.")

    fc1, fc2 = st.columns(2)
    with fc1:
        all_camps = ["(전체)"] + sorted(df_cre["campaign"].unique().tolist())
        sel_camp  = st.selectbox("📁 캠페인 필터", all_camps, key=f"{key_prefix}_camp")
    with fc2:
        ag_pool = df_cre if sel_camp == "(전체)" else df_cre[df_cre["campaign"] == sel_camp]
        all_ags = ["(전체)"] + sorted(ag_pool["adgroup"].unique().tolist())
        sel_ag  = st.selectbox("📂 광고그룹 필터", all_ags, key=f"{key_prefix}_ag")

    df_f = df_cre.copy()
    if sel_camp != "(전체)":
        df_f = df_f[df_f["campaign"] == sel_camp]
    if sel_ag != "(전체)":
        df_f = df_f[df_f["adgroup"] == sel_ag]
    if df_f.empty:
        st.warning("선택한 조건에 해당하는 소재가 없습니다.")
        return

    n_creatives = df_f["creative"].nunique()
    tot_imp     = int(df_f["impressions"].sum())
    tot_clk     = int(df_f["clicks"].sum())
    tot_cost    = float(df_f["cost"].sum())
    tot_conv    = float(df_f["conversions"].sum()) if "conversions" in df_f.columns else 0
    labels      = sorted(set(df_f["conv_label"].dropna().unique())) if "conv_label" in df_f.columns else []
    conv_label  = "/".join(labels) if labels else "CPA"

    k = st.columns(5 if show_conv else 4)
    k[0].metric("소재 수",  f"{n_creatives:,}")
    k[1].metric("노출",     f"{tot_imp:,}")
    k[2].metric("클릭",     f"{tot_clk:,}")
    k[3].metric("광고비",   f"₩{tot_cost:,.0f}")
    if show_conv:
        k[4].metric(f"전환 ({conv_label})", f"{tot_conv:,.0f}")

    st.divider()
    st.subheader("🎨 소재별 성과")

    df_f_funnel = _add_funnel_cols_to_df(df_f, funnel_steps) if funnel_steps else df_f
    funnel_agg  = {}
    if funnel_steps:
        for step in funnel_steps:
            fk = f"_funnel_{step['order']}"
            if fk in df_f_funnel.columns:
                funnel_agg[fk] = (fk, "sum")

    g = df_f_funnel.groupby("creative", as_index=False).agg(**{
        "impressions": ("impressions","sum"),
        "clicks":      ("clicks","sum"),
        "cost":        ("cost","sum"),
        **({} if not show_conv else {"conversions": ("conversions","sum")}),
        **funnel_agg,
    })

    g["CTR (%)"] = g.apply(lambda r: round(safe_div(r["clicks"], r["impressions"])*100, 2), axis=1)
    g["CPM (₩)"] = g.apply(lambda r: round(safe_div(r["cost"], r["impressions"])*1000), axis=1)
    g["CPC (₩)"] = g.apply(lambda r: round(safe_div(r["cost"], r["clicks"])),             axis=1)
    g["광고비"]   = g["cost"].astype(int)
    g["노출"]     = g["impressions"]
    g["클릭"]     = g["clicks"]
    base_cols     = ["creative","노출","클릭","광고비","CTR (%)","CPM (₩)","CPC (₩)"]

    if funnel_steps:
        g = _build_funnel_agg_cols(g, funnel_steps)
        g = _add_funnel_rate_cols(g, funnel_steps)

    funnel_labels = [s["label"] for s in sorted(funnel_steps, key=lambda x: x["order"])] if funnel_steps else []
    extra_cols    = _get_funnel_extra_cols(funnel_steps)

    if show_conv:
        g["전환"]              = g["conversions"].astype(int)
        g["CVR (%)"]           = g.apply(lambda r: round(safe_div(r["conversions"], r["clicks"])*100, 2), axis=1)
        g[f"{conv_label} (₩)"] = g.apply(
            lambda r: round(safe_div(r["cost"], r["conversions"])) if r["conversions"] else 0, axis=1)
        cols_show = base_cols + ["전환","CVR (%)", f"{conv_label} (₩)"] + funnel_labels + extra_cols
    else:
        cols_show = base_cols + funnel_labels + extra_cols

    cols_show = [c for c in cols_show if c in g.columns]
    show      = g[cols_show].rename(columns={"creative":"소재"}).sort_values("광고비", ascending=False)

    cre_col_config = {
        "노출":     st.column_config.NumberColumn("노출",     format="%,d"),
        "클릭":     st.column_config.NumberColumn("클릭",     format="%,d"),
        "광고비":   st.column_config.NumberColumn("광고비",   format="₩%,d"),
        "CTR (%)": st.column_config.NumberColumn("CTR (%)", format="%.2f%%"),
        "CPM (₩)": st.column_config.NumberColumn("CPM (₩)", format="₩%,d"),
        "CPC (₩)": st.column_config.NumberColumn("CPC (₩)", format="₩%,d"),
    }
    if show_conv:
        cre_col_config["전환"]    = st.column_config.NumberColumn("전환", format="%,d")
        cre_col_config["CVR (%)"] = st.column_config.NumberColumn("CVR (%)", format="%.2f%%")
        cre_col_config[f"{conv_label} (₩)"] = st.column_config.NumberColumn(
            f"{conv_label} (₩)", format="₩%,d")

    st.dataframe(
        _style_col_groups(show, conv_label),
        use_container_width=True, hide_index=True, column_config=cre_col_config
    )
    st.divider()

    cc1, cc2 = st.columns(2)
    with cc1:
        st.subheader("💰 소재별 광고비 TOP 15")
        top_cost = g.sort_values("cost", ascending=False).head(15)
        if not top_cost.empty:
            fig = px.bar(top_cost, x="cost", y="creative", orientation="h",
                         color_discrete_sequence=["#4285F4" if platform=="GOOGLE" else "#1877F2"])
            fig.update_layout(height=420, yaxis={"categoryorder":"total ascending"},
                              showlegend=False, margin=dict(l=10,r=10,t=20,b=20))
            st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_cost_chart")
    with cc2:
        st.subheader("🎯 소재별 CTR TOP 15")
        top_ctr = g[g["impressions"] >= 100].sort_values("CTR (%)", ascending=False).head(15)
        if not top_ctr.empty:
            fig = px.bar(top_ctr, x="CTR (%)", y="creative", orientation="h",
                         color_discrete_sequence=["#10B981"])
            fig.update_layout(height=420, yaxis={"categoryorder":"total ascending"},
                              showlegend=False, margin=dict(l=10,r=10,t=20,b=20))
            st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_ctr_chart")
        else:
            st.caption("노출 100회 이상인 소재가 없습니다.")

    st.divider()
    if adv_code:
        creative_images      = get_creative_images(adv_code, platform)
        creative_cost_order  = g.sort_values("cost", ascending=False)["creative"].tolist()
        render_creative_image_gallery(creative_images, creative_cost_order, key_prefix=key_prefix)

# ============================================================
# 이미지 업로드 섹션
# ============================================================
def render_image_upload_section(adv_code, user_email, can_edit):
    st.subheader("🖼️ 소재 이미지 업로드")
    if not can_edit:
        st.error("⛔ VIEWER 권한은 이미지를 업로드할 수 없습니다.")
        return

    img_pf    = st.radio("매체 선택", ["GOOGLE","FACEBOOK"], horizontal=True, key="img_up_pf")
    creatives = get_distinct_creatives(adv_code, img_pf)
    if not creatives:
        st.warning(f"💡 {img_pf} 매체에 인식된 소재명이 없습니다.")
        return

    existing_images = get_creative_images(adv_code, img_pf)

    st.markdown("#### ➕ 이미지 등록/수정")

    def fmt_cre(name):
        return ("✅ " if name in existing_images else "⬜ ") + name

    sel_creative = st.selectbox("소재명 선택", options=creatives,
                                format_func=fmt_cre, key="img_sel_creative")

    if sel_creative in existing_images:
        b64_data, media_type = existing_images[sel_creative]
        prev_col, _ = st.columns([1, 3])
        with prev_col:
            st.markdown(
                f'<img src="data:{media_type};base64,{b64_data}" '
                f'style="width:100%;border-radius:8px;border:1px solid #e5e7eb;'
                f'object-fit:contain;max-height:200px;" />',
                unsafe_allow_html=True)

    uploaded_img = st.file_uploader(
        "이미지 파일 선택 (JPG / PNG / GIF / WEBP)",
        type=["jpg","jpeg","png","gif","webp"],
        key=f"img_file_{img_pf}"
    )

    if uploaded_img:
        prev2_col, _ = st.columns([1, 3])
        with prev2_col:
            st.image(uploaded_img, use_container_width=True)

        ext_to_mime = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                       "png":"image/png","gif":"image/gif","webp":"image/webp"}
        ext        = uploaded_img.name.rsplit(".", 1)[-1].lower()
        media_type = ext_to_mime.get(ext, "image/jpeg")

        if st.button("💾 이미지 저장", type="primary", key="img_save_btn"):
            img_b64 = base64.b64encode(uploaded_img.read()).decode("utf-8")
            upsert_creative_image(adv_code, img_pf, sel_creative, img_b64, media_type, user_email)
            st.success(f"✅ '{sel_creative}' 이미지 저장 완료!")
            st.rerun()

    st.divider()
    st.markdown("#### 📦 파일명으로 일괄 업로드")
    bulk_files = st.file_uploader(
        "이미지 파일 여러 개 선택",
        type=["jpg","jpeg","png","gif","webp"],
        accept_multiple_files=True,
        key=f"img_bulk_{img_pf}"
    )

    if bulk_files:
        ext_to_mime   = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                         "png":"image/png","gif":"image/gif","webp":"image/webp"}
        creatives_set = set(creatives)
        matched, unmatched = [], []
        for f in bulk_files:
            fname_no_ext = f.name.rsplit(".", 1)[0]
            if fname_no_ext in creatives_set:
                matched.append((f, fname_no_ext))
            else:
                unmatched.append(f.name)

        st.markdown(f"**매핑 결과:** 자동 매핑 {len(matched)}개 · 미매핑 {len(unmatched)}개")

        if matched and st.button(f"💾 매핑된 {len(matched)}개 저장", type="primary", key="img_bulk_save"):
            for f, cname in matched:
                f.seek(0)
                ext        = f.name.rsplit(".", 1)[-1].lower()
                media_type = ext_to_mime.get(ext, "image/jpeg")
                img_b64    = base64.b64encode(f.read()).decode("utf-8")
                upsert_creative_image(adv_code, img_pf, cname, img_b64, media_type, user_email)
            st.success(f"✅ {len(matched)}개 저장 완료!")
            st.rerun()

        if unmatched:
            with st.expander(f"⚠️ 미매핑 {len(unmatched)}개"):
                for nm in unmatched:
                    st.markdown(f"- `{nm}`")

    st.divider()
    st.markdown("#### 📋 등록된 이미지 목록")
    existing_images_fresh = get_creative_images(adv_code, img_pf)
    if not existing_images_fresh:
        st.info("등록된 이미지가 없습니다.")
        return

    cols_per_row_mgmt = 4
    img_items = list(existing_images_fresh.items())
    for i in range(0, len(img_items), cols_per_row_mgmt):
        chunk = img_items[i: i + cols_per_row_mgmt]
        cols  = st.columns(cols_per_row_mgmt)
        for j, (cre_name, (b64_data, media_type)) in enumerate(chunk):
            img_src = f"data:{media_type};base64,{b64_data}"
            short   = cre_name if len(cre_name) <= 22 else cre_name[:20] + "…"
            with cols[j]:
                st.markdown(
                    f'<img src="{img_src}" style="width:100%;border-radius:6px;'
                    f'border:1px solid #e5e7eb;object-fit:contain;max-height:140px;" />',
                    unsafe_allow_html=True)
                st.caption(short)
                if st.button("🗑️ 삭제", key=f"img_del_{img_pf}_{i}_{j}", use_container_width=True):
                    delete_creative_image(adv_code, img_pf, cre_name)
                    st.success(f"'{cre_name}' 삭제됨")
                    st.rerun()


# ============================================================
# 페이지 라우팅
# ============================================================

# ============ 대시보드 ============
if page == "📈 대시보드" and adv_code:
    st.title(f"📈 {sel_name} — 성과 대시보드")

    raw = pd.read_sql(
        "SELECT * FROM perf WHERE advertiser_code = %(code)s",
        engine, params={"code": adv_code})

    if raw.empty:
        st.warning("데이터가 없습니다. '데이터 업로드' 메뉴에서 파일을 올리거나 API 연동을 설정하세요.")
        st.stop()

    raw["date"] = pd.to_datetime(raw["date"])
    min_d, max_d = raw["date"].min().date(), raw["date"].max().date()

    adv_row = q("SELECT total_budget, COALESCE(show_conversion,1) FROM advertisers WHERE code=?", (adv_code,))
    if adv_row:
        total_budget = float(adv_row[0][0] or 0)
        show_conv    = bool(adv_row[0][1])
    else:
        total_budget, show_conv = 0, True

    with st.form("dashboard_filter_form"):
        fc1, fc2 = st.columns([3, 1])
        with fc1:
            date_range = st.date_input("📅 기간 선택", value=(min_d, max_d),
                                       min_value=min_d, max_value=max_d)
        with fc2:
            st.markdown("<br>", unsafe_allow_html=True)
            submitted = st.form_submit_button("🔍 조회", type="primary", use_container_width=True)

    filter_key = f"dash_filter_{adv_code}"
    if submitted or filter_key not in st.session_state:
        st.session_state[filter_key] = date_range

    applied_range = st.session_state[filter_key]
    df_all = raw.copy()
    if isinstance(applied_range, tuple) and len(applied_range) == 2:
        d_from, d_to = applied_range
        df_all = df_all[(df_all["date"] >= pd.Timestamp(d_from)) &
                        (df_all["date"] <= pd.Timestamp(d_to))]

    mapping = get_conversion_mapping(adv_code)
    df_all  = compute_metrics(df_all, mapping)

    available     = sorted(df_all["platform"].unique(),
                           key=lambda x: {"GOOGLE":0,"FACEBOOK":1}.get(x, 99))
    cre_row       = q("SELECT COALESCE(show_creative,0) FROM advertisers WHERE code=?", (adv_code,))
    show_creative = bool(cre_row[0][0]) if cre_row else False

    tab_labels = ["📊 Summary"]
    tab_keys   = ["summary"]
    if "GOOGLE" in available:
        tab_labels.append("🟦 Google");       tab_keys.append("google")
        if show_creative:
            tab_labels.append("🎨 구글_광고소재"); tab_keys.append("google_cre")
    if "FACEBOOK" in available:
        tab_labels.append("🟪 Facebook");     tab_keys.append("facebook")
        if show_creative:
            tab_labels.append("🎨 페이스북_광고소재"); tab_keys.append("facebook_cre")

    tabs = st.tabs(tab_labels)
    tabd = dict(zip(tab_keys, tabs))

    with tabd["summary"]:
        st.markdown("##### 매체 선택")
        priority = {"GOOGLE":0,"FACEBOOK":1,"NAVER":2,"KAKAO":3,"TIKTOK":4}
        all_pfs  = sorted(available, key=lambda x: priority.get(x, 99))
        if not all_pfs:
            st.info("데이터 없음")
        else:
            cb_cols = st.columns([1,1,1,1,6])
            sel_pfs = []
            for i, p in enumerate(all_pfs):
                if cb_cols[i].checkbox(p, value=True, key=f"sum_pf_{p}"):
                    sel_pfs.append(p)
            df_s = df_all[df_all["platform"].isin(sel_pfs)] if sel_pfs else df_all.iloc[0:0]
            if df_s.empty:
                st.warning("선택한 매체에 데이터가 없습니다.")
            else:
                conv_label = render_kpi(df_s, total_budget, show_conv, key_suffix="sum")
                st.divider()
                cc1, cc2 = st.columns([1, 2])
                with cc1: chart_cost_donut(df_s)
                with cc2: chart_campaign_bar(df_s, "cost")
                st.divider()
                chart_daily_metric(df_s, conv_label, key_prefix="sum")
                st.divider()
                st.subheader("📋 캠페인별 효율")
                summary_funnel_steps = []
                seen_labels = set()
                for pf in sel_pfs:
                    for step in get_funnel_steps(adv_code, pf):
                        if step["label"] not in seen_labels:
                            summary_funnel_steps.append(step)
                            seen_labels.add(step["label"])
                render_campaign_table(df_s, conv_label, key="sum_camp",
                                      show_conversion=show_conv,
                                      funnel_steps=summary_funnel_steps or None)

    if "google" in tabd:
        with tabd["google"]:
            df_g            = df_all[df_all["platform"] == "GOOGLE"]
            funnel_steps_g  = get_funnel_steps(adv_code, "GOOGLE")
            conv_label      = render_kpi(df_g, total_budget, show_conv, key_suffix="g")
            st.divider()
            cc1, cc2 = st.columns([2, 1])
            with cc1:
                chart_daily_metric(df_g, conv_label, key_prefix="g")
            with cc2:
                by_c = df_g.groupby("campaign", as_index=False)["cost"].sum()
                fig  = px.pie(by_c, names="campaign", values="cost", hole=0.4,
                              title="캠페인별 광고비",
                              color_discrete_sequence=px.colors.sequential.Blues_r)
                fig.update_layout(height=380)
                st.plotly_chart(fig, use_container_width=True, key="g_camp_pie")
            st.divider()
            st.subheader("📋 캠페인별 성과")
            if funnel_steps_g:
                st.caption(f"🪜 퍼널: {' → '.join([s['label'] for s in sorted(funnel_steps_g, key=lambda x: x['order'])])}")
            render_campaign_table(df_g, conv_label, key="g_camp",
                                  show_conversion=show_conv, funnel_steps=funnel_steps_g)
            st.divider()
            st.subheader("📁 캠페인별 광고그룹 성과")
            for camp in sorted(df_g["campaign"].unique()):
                sub = df_g[df_g["campaign"] == camp]
                with st.expander(f"📁 {camp}  (₩{sub['cost'].sum():,.0f} · {int(sub['impressions'].sum()):,})"):
                    render_adgroup_table(sub, conv_label, key=f"g_ag_{camp}",
                                         show_conversion=show_conv, funnel_steps=funnel_steps_g)

    if "google_cre" in tabd:
        with tabd["google_cre"]:
            df_g           = df_all[df_all["platform"] == "GOOGLE"]
            funnel_steps_g = get_funnel_steps(adv_code, "GOOGLE")
            render_creative_tab(df_g, "GOOGLE", key_prefix="g_cre", show_conv=show_conv,
                                funnel_steps=funnel_steps_g, adv_code=adv_code)

    if "facebook" in tabd:
        with tabd["facebook"]:
            df_f2          = df_all[df_all["platform"] == "FACEBOOK"]
            funnel_steps_f = get_funnel_steps(adv_code, "FACEBOOK")
            conv_label     = render_kpi(df_f2, total_budget, show_conv, key_suffix="f")
            st.divider()
            cc1, cc2 = st.columns([2, 1])
            with cc1:
                chart_daily_metric(df_f2, conv_label, key_prefix="f")
            with cc2:
                by_c = df_f2.groupby("campaign", as_index=False)["cost"].sum()
                fig  = px.pie(by_c, names="campaign", values="cost", hole=0.4,
                              title="캠페인별 광고비",
                              color_discrete_sequence=px.colors.sequential.Purples_r)
                fig.update_layout(height=380)
                st.plotly_chart(fig, use_container_width=True, key="f_camp_pie")
            st.divider()
            st.subheader("📋 캠페인별 성과")
            if funnel_steps_f:
                st.caption(f"🪜 퍼널: {' → '.join([s['label'] for s in sorted(funnel_steps_f, key=lambda x: x['order'])])}")
            render_campaign_table(df_f2, conv_label, key="f_camp",
                                  show_conversion=show_conv, funnel_steps=funnel_steps_f)
            st.divider()
            st.subheader("📁 캠페인별 광고그룹 성과")
            for camp in sorted(df_f2["campaign"].unique()):
                sub = df_f2[df_f2["campaign"] == camp]
                with st.expander(f"📁 {camp}  (₩{sub['cost'].sum():,.0f} · {int(sub['impressions'].sum()):,})"):
                    render_adgroup_table(sub, conv_label, key=f"f_ag_{camp}",
                                         show_conversion=show_conv, funnel_steps=funnel_steps_f)

    if "facebook_cre" in tabd:
        with tabd["facebook_cre"]:
            df_f2          = df_all[df_all["platform"] == "FACEBOOK"]
            funnel_steps_f = get_funnel_steps(adv_code, "FACEBOOK")
            render_creative_tab(df_f2, "FACEBOOK", key_prefix="f_cre", show_conv=show_conv,
                                funnel_steps=funnel_steps_f, adv_code=adv_code)


# ============================================================
# 데이터 업로드 (탭 3개: 파일 / API / 이미지)
# ============================================================
elif page == "📤 데이터 업로드" and adv_code:
    st.title("📤 데이터 업로드")
    if my_level == "VIEWER":
        st.error("⛔ VIEWER 권한은 업로드할 수 없습니다.")
        st.stop()

    can_edit = (my_level in ("OWNER","EDITOR")) or is_admin

    up_tab1, up_tab2, up_tab3 = st.tabs([
        "📊 파일 업로드",
        "🔌 API 자동 연동",
        "🖼️ 소재 이미지 업로드",
    ])

    # ── 탭1: 파일 업로드 ───────────────────────────────────
    with up_tab1:
        st.markdown("### 📊 로우데이터 업로드")
        platform = st.radio("매체 선택", ["GOOGLE","FACEBOOK"], horizontal=True, key="up_pf")
        file     = st.file_uploader("파일 업로드 (xlsx / csv)", type=["xlsx","xls","csv"], key="up_file")

        if file:
            cur_sig = f"{file.name}_{file.size}"
            if st.session_state.get("up_sig") != cur_sig:
                st.session_state["up_sig"] = cur_sig
                for k in ["upload_df","upload_other"]:
                    if k in st.session_state:
                        del st.session_state[k]

        if file:
            try:
                df_raw = read_uploaded_file(file)
                st.success(f"✅ 파일 읽기 완료 — {len(df_raw):,}행 · {len(df_raw.columns)}개 컬럼")
                with st.expander("📋 원본 미리보기 (상위 5행)", expanded=False):
                    st.dataframe(df_raw.head(5), use_container_width=True)

                st.divider()
                st.subheader("🔗 컬럼 매핑")

                cols = ["(선택안함)"] + list(df_raw.columns)
                def safe_idx(guess):
                    return cols.index(guess) if guess in cols else 0

                g_date = guess_column(df_raw.columns, DATE_CANDS)
                g_camp = guess_column(df_raw.columns, CAMP_CANDS)
                g_ag   = guess_column(df_raw.columns, AG_CANDS)
                g_imp  = guess_column(df_raw.columns, IMP_CANDS)
                g_clk  = guess_column(df_raw.columns, CLK_CANDS)
                g_cost = guess_column(df_raw.columns, COST_CANDS)
                g_cre  = guess_column(df_raw.columns, CREATIVE_CANDS)

                mc1, mc2, mc3 = st.columns(3)
                with mc1:
                    col_date = st.selectbox("📅 일자 *",  cols, index=safe_idx(g_date), key="map_date")
                    col_imp  = st.selectbox("👁️ 노출수 *", cols, index=safe_idx(g_imp),  key="map_imp")
                with mc2:
                    col_camp = st.selectbox("📁 캠페인 *", cols, index=safe_idx(g_camp), key="map_camp")
                    col_clk  = st.selectbox("🖱️ 클릭수 *", cols, index=safe_idx(g_clk),  key="map_clk")
                with mc3:
                    col_ag   = st.selectbox("📂 광고그룹", cols, index=safe_idx(g_ag),   key="map_ag")
                    col_cost = st.selectbox("💰 비용 *",   cols, index=safe_idx(g_cost), key="map_cost")

                st.markdown("##### 🎨 광고 소재 (선택)")
                scol1, scol2 = st.columns([1, 2])
                with scol1:
                    use_creative = st.checkbox("소재 데이터 포함", value=bool(g_cre), key="map_use_creative")
                with scol2:
                    col_cre = st.selectbox("🎨 광고 소재 컬럼", cols,
                        index=safe_idx(g_cre), key="map_cre",
                        label_visibility="collapsed") if use_creative else "(선택안함)"

                st.markdown("##### 🎯 전환 지표 (선택)")
                conv_col1, conv_col2, conv_col3 = st.columns([1, 2, 1.5])
                with conv_col1:
                    use_conv_map = st.checkbox("전환 컬럼 지정", value=False, key="map_use_conv")
                with conv_col2:
                    col_conv = st.selectbox("🎯 전환 컬럼", cols, key="map_conv",
                        label_visibility="collapsed") if use_conv_map else "(선택안함)"
                with conv_col3:
                    conv_label_input = st.selectbox("표시 레이블",
                        ["CPI","CPA","CPL","CPV","CPE"], key="map_conv_label",
                        label_visibility="collapsed") if use_conv_map else "CPA"

                cost_unit = "KRW (원본 그대로)"
                if platform == "FACEBOOK":
                    cost_unit = st.radio("💱 비용 통화",
                        ["KRW (원본 그대로)","USD → KRW 환산 (× 1,300)"],
                        horizontal=True, key="map_currency",
                        index=1 if (col_cost and "USD" in col_cost.upper()) else 0)

                mapped = {col_date, col_camp, col_ag, col_imp, col_clk, col_cost, col_cre}
                mapped.discard("(선택안함)")
                other_numeric = []
                for c in df_raw.columns:
                    if c in mapped:
                        continue
                    try:
                        pd.to_numeric(df_raw[c], errors="raise")
                        other_numeric.append(c)
                    except:
                        continue

                if other_numeric:
                    st.caption(f"📌 raw_data에 저장될 전환 후보 컬럼: **{', '.join(other_numeric)}**")

                st.divider()
                st.subheader("📦 업로드 방식")
                mode = st.radio("업로드 모드",
                    ["① 추가 (Append)",
                     "② 기간 덮어쓰기 (Upsert by Date) — 권장",
                     "③ 매체 전체 초기화 (Replace All)"],
                    index=1, key="up_mode")

                if mode.startswith("①"):
                    st.info("**① 추가** — 기존 데이터를 두고 새 행을 덧붙입니다.")
                elif mode.startswith("②"):
                    st.success("**② 기간 덮어쓰기** — 파일에 포함된 날짜 범위의 기존 데이터를 삭제 후 교체합니다.")
                else:
                    st.error("**③ 전체 초기화** — 해당 매체의 모든 기존 데이터를 삭제합니다.")

                st.divider()

                if st.button("🔄 변환 실행 & 미리보기", type="secondary"):
                    required = {"일자":col_date,"캠페인":col_camp,"노출수":col_imp,"클릭수":col_clk,"비용":col_cost}
                    missing  = [k for k, v in required.items() if v == "(선택안함)"]
                    if missing:
                        st.error(f"❌ 필수 항목 미지정: {', '.join(missing)}")
                    else:
                        df = pd.DataFrame(index=df_raw.index)
                        df["date"]        = pd.to_datetime(df_raw[col_date], errors="coerce").dt.strftime("%Y-%m-%d")
                        df["campaign"]    = df_raw[col_camp].astype(str)
                        df["adgroup"]     = df_raw[col_ag].astype(str) if col_ag != "(선택안함)" else df["campaign"]
                        df["impressions"] = pd.to_numeric(df_raw[col_imp],  errors="coerce").fillna(0).astype(int)
                        df["clicks"]      = pd.to_numeric(df_raw[col_clk],  errors="coerce").fillna(0).astype(int)
                        df["cost"]        = pd.to_numeric(df_raw[col_cost], errors="coerce").fillna(0)
                        if cost_unit.startswith("USD"):
                            df["cost"] = df["cost"] * 1300
                        df["creative"] = df_raw[col_cre].astype(str) if col_cre != "(선택안함)" else None

                        def make_raw(idx):
                            d = {}
                            for c in other_numeric:
                                v = df_raw.loc[idx, c]
                                try:
                                    d[c] = float(v) if pd.notna(v) else 0
                                except:
                                    d[c] = 0
                            return json.dumps(d, ensure_ascii=False)

                        df["raw_data"] = [make_raw(i) for i in df.index]
                        df = df.dropna(subset=["date"])

                        if df.empty:
                            st.error("❌ 일자 컬럼이 인식되지 않았습니다.")
                        else:
                            st.session_state["upload_df"]          = df.reset_index(drop=True)
                            st.session_state["upload_other"]       = other_numeric
                            st.session_state["upload_conv_col"]    = col_conv if use_conv_map else "(선택안함)"
                            st.session_state["upload_conv_label"]  = conv_label_input if use_conv_map else "CPA"
                            st.success(f"✅ 변환 완료 — {len(df):,}행")

                if "upload_df" in st.session_state:
                    df = st.session_state["upload_df"]
                    st.subheader("📊 변환된 데이터 미리보기")
                    st.dataframe(df.head(8), use_container_width=True, hide_index=True)

                    kc = st.columns(4)
                    kc[0].metric("총 행수", f"{len(df):,}")
                    kc[1].metric("총 노출", f"{int(df['impressions'].sum()):,}")
                    kc[2].metric("총 클릭", f"{int(df['clicks'].sum()):,}")
                    kc[3].metric("총 비용", f"₩{float(df['cost'].sum()):,.0f}")

                    if mode.startswith("②"):
                        dates_in_file = sorted(set(df["date"].dropna().tolist()))
                        if dates_in_file:
                            with engine.connect() as con:
                                placeholders = ",".join([f":d{i}" for i in range(len(dates_in_file))])
                                result = con.execute(text(
                                    f"SELECT COUNT(*) FROM perf "
                                    f"WHERE advertiser_code=:adv AND platform=:pf "
                                    f"AND date IN ({placeholders})"
                                ), {"adv": adv_code, "pf": platform,
                                    **{f"d{i}": d for i, d in enumerate(dates_in_file)}})
                                will_delete = result.fetchone()[0]
                        else:
                            will_delete = 0
                        st.warning(f"⚠️ 기존 {will_delete:,}행 삭제 후 {len(df):,}행으로 교체됩니다.")
                    elif mode.startswith("③"):
                        wd = q("SELECT COUNT(*) FROM perf WHERE advertiser_code=? AND platform=?",
                               (adv_code, platform))
                        will_delete = wd[0][0] if wd else 0
                        st.error(f"🚨 {platform} 전체 {will_delete:,}행 삭제 후 {len(df):,}행으로 교체됩니다.")

                    proceed = True
                    if mode.startswith("③"):
                        confirm_text = st.text_input(
                            f"전체 초기화를 진행하려면 **{platform}** 을(를) 입력하세요",
                            key="confirm_replace")
                        proceed = (confirm_text.strip().upper() == platform)
                        if not proceed and confirm_text:
                            st.warning("입력값이 일치하지 않습니다.")

                    btn_label = {"①":"💾 추가 저장","②":"💾 덮어쓰기 저장","③":"🚨 초기화 후 저장"}[mode[0]]

                    if st.button(btn_label, type="primary", disabled=not proceed):
                        try:
                            with engine.connect() as con:
                                deleted = 0
                                if mode.startswith("②"):
                                    dates_in_file = sorted(set(df["date"].dropna().tolist()))
                                    if dates_in_file:
                                        placeholders = ",".join([f":del_d{i}" for i in range(len(dates_in_file))])
                                        result = con.execute(text(
                                            f"DELETE FROM perf "
                                            f"WHERE advertiser_code=:adv AND platform=:pf "
                                            f"AND date IN ({placeholders})"
                                        ), {"adv": adv_code, "pf": platform,
                                            **{f"del_d{i}": d for i, d in enumerate(dates_in_file)}})
                                        deleted = result.rowcount
                                elif mode.startswith("③"):
                                    result  = con.execute(text(
                                        "DELETE FROM perf WHERE advertiser_code=:adv AND platform=:pf"
                                    ), {"adv": adv_code, "pf": platform})
                                    deleted = result.rowcount

                                result2 = con.execute(text("""
                                    INSERT INTO upload_log
                                        (email, advertiser_code, platform, file_name,
                                         rows, uploaded_at, upload_mode, deleted_rows)
                                    VALUES (:email,:adv,:pf,:fn,:rows,:ts,:mode,:del)
                                    RETURNING id
                                """), {
                                    "email": user["email"], "adv": adv_code, "pf": platform,
                                    "fn": file.name, "rows": len(df),
                                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "mode": mode, "del": deleted
                                })
                                upload_id = result2.fetchone()[0]

                                rows_to_insert = []
                                for _, row in df.iterrows():
                                    cre_val = (row["creative"]
                                               if "creative" in df.columns and pd.notna(row["creative"])
                                               else None)
                                    rows_to_insert.append({
                                        "adv":  adv_code, "pf":   platform,
                                        "date": row["date"], "camp": row["campaign"],
                                        "ag":   row["adgroup"],
                                        "imp":  int(row["impressions"]),
                                        "clk":  int(row["clicks"]),
                                        "cost": float(row["cost"]),
                                        "raw":  row["raw_data"],
                                        "uid":  upload_id, "cre":  cre_val,
                                    })

                                con.execute(text("""
                                    INSERT INTO perf
                                        (advertiser_code, platform, date, campaign, adgroup,
                                         impressions, clicks, cost, raw_data, upload_log_id, creative)
                                    VALUES
                                        (:adv,:pf,:date,:camp,:ag,:imp,:clk,:cost,:raw,:uid,:cre)
                                """), rows_to_insert)
                                con.commit()

                            saved_conv_col   = st.session_state.get("upload_conv_col", "(선택안함)")
                            saved_conv_label = st.session_state.get("upload_conv_label", "CPA")
                            if saved_conv_col and saved_conv_col != "(선택안함)":
                                q("""
                                    INSERT INTO conversion_mapping
                                        (advertiser_code, platform, campaign,
                                         conversion_column, conversion_label, updated_at)
                                    VALUES (?,?,?,?,?,?)
                                    ON CONFLICT (advertiser_code, platform, campaign) DO UPDATE SET
                                        conversion_column = EXCLUDED.conversion_column,
                                        conversion_label  = EXCLUDED.conversion_label,
                                        updated_at        = EXCLUDED.updated_at
                                """, (adv_code, platform, "*",
                                      saved_conv_col, saved_conv_label,
                                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")), fetch=False)

                            msg = (f"🎉 완료! {deleted:,}행 삭제 → {len(df):,}행 저장"
                                   if deleted else f"🎉 {len(df):,}행 저장 완료!")
                            st.success(msg)
                            for k in ["upload_df","upload_other","up_sig",
                                      "upload_conv_col","upload_conv_label"]:
                                if k in st.session_state:
                                    del st.session_state[k]
                            st.balloons()

                        except Exception as e:
                            st.error(f"❌ 저장 실패: {e}")
                            import traceback; st.code(traceback.format_exc())

            except Exception as e:
                st.error(f"파일 처리 오류: {e}")
                import traceback; st.code(traceback.format_exc())

    # ── 탭2: API 자동 연동 ─────────────────────────────────
    with up_tab2:
        render_api_sync_tab(adv_code, user["email"], can_edit)

    # ── 탭3: 이미지 업로드 ─────────────────────────────────
    with up_tab3:
        render_image_upload_section(adv_code, user["email"], can_edit)


# ============================================================
# 업로드 이력
# ============================================================
elif page == "📋 업로드 이력" and adv_code:
    st.title("📋 업로드 이력")
    st.caption("각 업로드 행 우측의 🗑️ 버튼으로 개별 삭제할 수 있습니다.")

    logs_raw = q("""
        SELECT id, uploaded_at, email, platform, file_name, rows,
               COALESCE(upload_mode,'(legacy)'), COALESCE(deleted_rows,0)
        FROM upload_log WHERE advertiser_code=? ORDER BY id DESC
    """, (adv_code,))

    if not logs_raw:
        st.info("업로드 이력이 없습니다.")
        st.stop()

    can_delete = (my_level in ("OWNER","EDITOR")) or is_admin

    legacy_count_per_pf = {}
    for row in logs_raw:
        if row[6] == "(legacy)":
            legacy_count_per_pf[row[3]] = legacy_count_per_pf.get(row[3], 0) + 1

    h = st.columns([0.7,1.8,2.4,1,2.6,0.9,1.4,0.7])
    for label, col in zip(
        ["**ID**","**업로드 시각**","**사용자**","**매체**","**파일명**","**현재 행수**","**모드**","**삭제**"], h
    ):
        col.markdown(label)
    st.markdown("<hr style='margin:4px 0;border-color:#e5e7eb'>", unsafe_allow_html=True)

    for row in logs_raw:
        log_id, ts, email, pf, fname, rows, mode_str, del_rows = row

        if mode_str == "(legacy)":
            cur_rows = q("""SELECT COUNT(*) FROM perf
                            WHERE advertiser_code=? AND platform=? AND upload_log_id IS NULL""",
                         (adv_code, pf))[0][0]
            unresolvable = legacy_count_per_pf.get(pf, 0) > 1
        else:
            cr = q("SELECT COUNT(*) FROM perf WHERE upload_log_id=?", (log_id,))
            cur_rows     = cr[0][0] if cr else 0
            unresolvable = False

        c = st.columns([0.7,1.8,2.4,1,2.6,0.9,1.4,0.7])
        c[0].markdown(f"`#{log_id}`")
        c[1].markdown(f"<span style='font-size:13px'>{ts}</span>", unsafe_allow_html=True)
        c[2].markdown(f"<span style='font-size:13px'>{email}</span>", unsafe_allow_html=True)
        c[3].markdown(f"`{pf}`")
        fname_short = fname if len(fname) <= 28 else fname[:25] + "..."
        c[4].markdown(f"<span style='font-size:13px'>{fname_short}</span>", unsafe_allow_html=True)
        c[5].markdown(f"**{cur_rows:,}**")
        mode_short = (mode_str.replace("(Append)","").replace("(Upsert by Date)","")
                              .replace("(Replace All)","").replace(" — 권장","").strip())
        c[6].markdown(f"<span style='font-size:12px'>{mode_short}</span>", unsafe_allow_html=True)

        if can_delete:
            if c[7].button("🗑️", key=f"del_btn_{log_id}"):
                st.session_state["pending_delete"] = log_id
                st.rerun()
        else:
            c[7].markdown("—")

    if not can_delete:
        st.caption("ℹ️ 삭제는 OWNER / EDITOR 권한자만 가능합니다.")
        st.stop()

    pid = st.session_state.get("pending_delete")
    if pid:
        sel = next((r for r in logs_raw if r[0] == pid), None)
        if not sel:
            st.session_state.pop("pending_delete", None)
            st.rerun()

        log_id, ts, email_log, pf, fname, rows, mode_str, _ = sel

        if mode_str == "(legacy)":
            cur_rows     = q("""SELECT COUNT(*) FROM perf
                                WHERE advertiser_code=? AND platform=? AND upload_log_id IS NULL""",
                             (adv_code, pf))[0][0]
            unresolvable = legacy_count_per_pf.get(pf, 0) > 1
        else:
            cr           = q("SELECT COUNT(*) FROM perf WHERE upload_log_id=?", (log_id,))
            cur_rows     = cr[0][0] if cr else 0
            unresolvable = False

        st.divider()
        st.markdown(
            f"""<div style='background:#fef3c7;border-left:4px solid #f59e0b;
            padding:14px 16px;border-radius:6px'>
            <strong>🗑️ 삭제 확인 — 업로드 #{log_id}</strong><br><br>
            <ul style='margin:0;padding-left:20px;font-size:14px'>
              <li>업로드 시각: <code>{ts}</code></li>
              <li>업로더: <code>{email_log}</code></li>
              <li>매체: <code>{pf}</code> · 파일명: <code>{fname}</code></li>
              <li>현재 DB 잔여: <strong>{cur_rows:,}행</strong></li>
            </ul></div>""",
            unsafe_allow_html=True)

        if mode_str == "(legacy)" and unresolvable:
            st.error("⚠️ legacy 업로드가 여러 건이라 자동 구분 불가합니다.")
            ec1, ec2, _ = st.columns([1.2, 1, 4])
            with ec1:
                if st.button("📝 이력 레코드만 삭제", key=f"legacy_log_only_{pid}"):
                    q("DELETE FROM upload_log WHERE id=?", (pid,), fetch=False)
                    st.session_state.pop("pending_delete", None)
                    st.success("이력 레코드 삭제 완료"); st.rerun()
            with ec2:
                if st.button("❌ 취소", key=f"legacy_cancel_{pid}"):
                    st.session_state.pop("pending_delete", None); st.rerun()
        else:
            if cur_rows == 0:
                st.info("💡 데이터는 이미 비어있습니다. 이력 레코드만 제거됩니다.")

            cc1, cc2, _ = st.columns([1, 1, 4])
            with cc1:
                if st.button("✅ 삭제 확정", type="primary", key=f"confirm_{pid}"):
                    try:
                        with engine.connect() as con:
                            if mode_str == "(legacy)":
                                result = con.execute(text(
                                    "DELETE FROM perf WHERE advertiser_code=:adv "
                                    "AND platform=:pf AND upload_log_id IS NULL"
                                ), {"adv": adv_code, "pf": pf})
                            else:
                                result = con.execute(text(
                                    "DELETE FROM perf WHERE upload_log_id=:uid"
                                ), {"uid": log_id})
                            deleted_cnt = result.rowcount
                            con.execute(text("DELETE FROM upload_log WHERE id=:uid"), {"uid": log_id})
                            con.commit()
                        st.session_state.pop("pending_delete", None)
                        st.success(f"✅ {deleted_cnt:,}행 + 이력 레코드 삭제 완료")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ 삭제 실패: {e}")
                        import traceback; st.code(traceback.format_exc())
            with cc2:
                if st.button("❌ 취소", key=f"cancel_{pid}"):
                    st.session_state.pop("pending_delete", None); st.rerun()


# ============ 전환지표 설정 ============
elif page == "🎯 전환지표 설정" and adv_code:
    st.title("🎯 전환지표 매핑 설정")

    cur_map = pd.read_sql("""
        SELECT platform AS 매체, campaign AS 캠페인,
               conversion_column AS 전환컬럼, conversion_label AS 라벨,
               updated_at AS 수정시각
        FROM conversion_mapping WHERE advertiser_code=%(code)s
        ORDER BY platform, campaign
    """, engine, params={"code": adv_code})

    st.subheader("📌 현재 매핑")
    if cur_map.empty:
        st.info("아직 매핑이 없습니다.")
    else:
        st.dataframe(cur_map, use_container_width=True, hide_index=True)

    st.divider()
    if my_level in ("OWNER","EDITOR") or is_admin:
        st.subheader("➕ 매핑 추가/수정")
        raw = pd.read_sql("""
            SELECT platform, campaign, raw_data FROM perf WHERE advertiser_code=%(code)s
        """, engine, params={"code": adv_code})

        with st.form("conv_mapping_form"):
            c1, c2 = st.columns(2)
            with c1:
                sel_pf = st.selectbox("매체", ["GOOGLE","FACEBOOK"])
            with c2:
                camps = ["* (이 매체의 기본값)"] + sorted(
                    raw[raw["platform"] == sel_pf]["campaign"].dropna().unique().tolist())
                sel_camp = st.selectbox("캠페인", camps)

            conv_keys = set()
            for rd in raw[raw["platform"] == sel_pf]["raw_data"].dropna():
                try:
                    conv_keys.update(json.loads(rd).keys())
                except:
                    pass
            conv_keys = sorted(conv_keys)

            if not conv_keys:
                st.warning(f"{sel_pf} 데이터가 없거나 전환 후보 컬럼이 없습니다.")
                st.form_submit_button("💾 저장", disabled=True)
            else:
                c3, c4 = st.columns(2)
                with c3:
                    sel_col = st.selectbox("전환으로 사용할 컬럼", conv_keys)
                with c4:
                    sel_lbl = st.selectbox("표시 라벨", ["CPI","CPA","CPL","CPV","CPE"])
                if st.form_submit_button("💾 저장", type="primary"):
                    sel_camp_val = "*" if sel_camp.startswith("*") else sel_camp
                    q("""
                        INSERT INTO conversion_mapping
                            (advertiser_code, platform, campaign,
                             conversion_column, conversion_label, updated_at)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT (advertiser_code, platform, campaign) DO UPDATE SET
                            conversion_column = EXCLUDED.conversion_column,
                            conversion_label  = EXCLUDED.conversion_label,
                            updated_at        = EXCLUDED.updated_at
                    """, (adv_code, sel_pf, sel_camp_val, sel_col, sel_lbl,
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S")), fetch=False)
                    st.success("저장됨")
                    st.rerun()

        st.divider()
        st.subheader("🗑️ 매핑 삭제")
        if not cur_map.empty:
            with st.form("conv_del_form"):
                del_idx = st.selectbox("삭제할 매핑", cur_map.index,
                    format_func=lambda i: (
                        f"{cur_map.iloc[i]['매체']} / {cur_map.iloc[i]['캠페인']} → "
                        f"{cur_map.iloc[i]['전환컬럼']}({cur_map.iloc[i]['라벨']})"))
                if st.form_submit_button("🗑️ 삭제 확인", type="primary"):
                    row = cur_map.iloc[del_idx]
                    q("DELETE FROM conversion_mapping WHERE advertiser_code=? AND platform=? AND campaign=?",
                      (adv_code, row["매체"], row["캠페인"]), fetch=False)
                    st.success("삭제됨")
                    st.rerun()

    st.divider()
    st.title("🪜 퍼널 단계 설정")
    st.markdown("""
**다단계 전환을 성과 테이블에 통합해서 분석할 수 있습니다.**
- ⭐ 라벨에 **"전환값"** 포함 시 CVR/CPA 대신 **ROAS** 자동 표시
""")

    fpf        = st.radio("매체 선택", ["GOOGLE","FACEBOOK"], horizontal=True, key="funnel_pf")
    avail_cols = get_raw_data_columns(adv_code, fpf)

    if not avail_cols:
        st.info(f"💡 {fpf} 매체에 raw_data 컬럼이 없습니다. 데이터를 먼저 업로드하세요.")
    else:
        st.caption(f"📌 사용 가능한 컬럼: {', '.join(avail_cols)}")
        sk = f"funnel_steps_{adv_code}_{fpf}"
        if sk not in st.session_state:
            cur_steps = get_funnel_steps(adv_code, fpf)
            st.session_state[sk] = [
                {"column": s["column"], "label": s["label"], "cvr_base": s["cvr_base"]}
                for s in cur_steps]

        steps = st.session_state[sk]

        if steps:
            h = st.columns([0.4,2.5,2.2,1.6,0.5])
            h[0].markdown("**#**"); h[1].markdown("**컬럼**")
            h[2].markdown("**라벨**"); h[3].markdown("**CVR 기준**"); h[4].markdown("**삭제**")

        new_steps        = []
        delete_triggered = False
        for i, step in enumerate(steps):
            cr = st.columns([0.4,2.5,2.2,1.6,0.5])
            cr[0].markdown(f"**{i+1}**")
            sel_col   = cr[1].selectbox(f"col_{i}", avail_cols,
                index=avail_cols.index(step["column"]) if step["column"] in avail_cols else 0,
                key=f"{sk}_col_{i}", label_visibility="collapsed")
            sel_label = cr[2].text_input(f"lab_{i}", value=step["label"],
                key=f"{sk}_lab_{i}", label_visibility="collapsed")
            is_roas   = _is_roas_step(sel_label) or _is_roas_step(sel_col)
            if is_roas:
                cr[3].markdown("🔄 *ROAS 자동*")
                sel_base = "clicks"
            else:
                sel_base = cr[3].selectbox(f"base_{i}", ["clicks","previous"],
                    index=0 if step["cvr_base"] == "clicks" else 1,
                    format_func=lambda x: "클릭 대비" if x == "clicks" else "이전 단계 대비",
                    key=f"{sk}_base_{i}", label_visibility="collapsed")
            del_clicked = cr[4].button("🗑️", key=f"{sk}_del_{i}")
            if del_clicked:
                delete_triggered = True
            else:
                new_steps.append({"column": sel_col, "label": sel_label, "cvr_base": sel_base})

        if delete_triggered:
            st.session_state[sk] = new_steps
            st.rerun()

        bc1, bc2, _ = st.columns([1, 1, 4])
        if bc1.button("➕ 단계 추가", key=f"{sk}_add"):
            st.session_state[sk] = new_steps + [{"column": avail_cols[0], "label": "", "cvr_base": "clicks"}]
            st.rerun()

        if new_steps and bc2.button("💾 저장", type="primary", key=f"{sk}_save"):
            empty = [i+1 for i, s in enumerate(new_steps) if not s["label"].strip()]
            if empty:
                st.error(f"❌ 라벨이 비어있는 단계: {empty}")
            else:
                st.session_state[sk] = new_steps
                save_funnel_steps(adv_code, fpf, new_steps)
                st.success(f"✅ {fpf} 매체 퍼널 {len(new_steps)}단계 저장 완료")
                st.rerun()


# ============ PDF 리포트 ============
elif page == "📥 PDF 리포트" and adv_code:
    st.title("📥 PDF 리포트 다운로드")

    raw = pd.read_sql("SELECT * FROM perf WHERE advertiser_code=%(code)s",
                      engine, params={"code": adv_code})
    if raw.empty:
        st.warning("데이터가 없습니다."); st.stop()
    raw["date"] = pd.to_datetime(raw["date"])

    adv_row      = q("SELECT name, total_budget, COALESCE(show_conversion,1) FROM advertisers WHERE code=?", (adv_code,))
    adv_name     = adv_row[0][0] if adv_row else adv_code
    total_budget = float(adv_row[0][1] or 0) if adv_row else 0
    show_conv    = bool(adv_row[0][2]) if adv_row else True
    min_d, max_d = raw["date"].min().date(), raw["date"].max().date()

    with st.form("pdf_filter_form"):
        c1, c2 = st.columns(2)
        with c1:
            d_range = st.date_input("📅 리포트 기간", value=(min_d, max_d),
                                    min_value=min_d, max_value=max_d)
        with c2:
            all_pfs = sorted(raw["platform"].unique())
            sel_pfs = st.multiselect("매체", all_pfs, default=all_pfs)
        filter_ok = st.form_submit_button("🔍 미리보기 조회", type="primary")

    pdf_filter_key = f"pdf_filter_{adv_code}"
    if filter_ok or pdf_filter_key not in st.session_state:
        st.session_state[pdf_filter_key] = {"d_range": d_range, "sel_pfs": sel_pfs}

    applied = st.session_state[pdf_filter_key]
    df_rep  = raw.copy()
    ar      = applied["d_range"]
    if isinstance(ar, tuple) and len(ar) == 2:
        df_rep = df_rep[(df_rep["date"] >= pd.Timestamp(ar[0])) &
                        (df_rep["date"] <= pd.Timestamp(ar[1]))]
    if applied["sel_pfs"]:
        df_rep = df_rep[df_rep["platform"].isin(applied["sel_pfs"])]
    if df_rep.empty:
        st.warning("선택한 조건에 데이터가 없습니다."); st.stop()

    mapping = get_conversion_mapping(adv_code)
    df_rep  = compute_metrics(df_rep, mapping)

    pc = st.columns(4)
    pc[0].metric("기간 행 수", f"{len(df_rep):,}")
    pc[1].metric("총 노출",    f"{int(df_rep['impressions'].sum()):,}")
    pc[2].metric("총 클릭",    f"{int(df_rep['clicks'].sum()):,}")
    pc[3].metric("총 광고비",  f"₩{float(df_rep['cost'].sum()):,.0f}")

    st.divider()
    if st.button("PDF 생성", type="primary"):
        with st.spinner("PDF 생성 중..."):
            try:
                pdf_bytes = build_pdf_report(adv_code, adv_name, df_rep, total_budget, show_conv)
                fname_base = f"{adv_code}_report_{datetime.now().strftime('%Y%m%d_%H%M')}"
                st.download_button("⬇️ PDF 다운로드", data=pdf_bytes,
                                   file_name=f"{fname_base}.pdf",
                                   mime="application/pdf")
                st.success("생성 완료!")
            except Exception as e:
                st.error(f"PDF 생성 실패: {e}")
                import traceback; st.code(traceback.format_exc())


# ============ 광고주 관리 ============
elif page == "🏢 광고주 관리":
    st.title("🏢 광고주 관리")
    if not is_admin:
        st.error("관리자 권한 필요"); st.stop()

    advs = pd.read_sql("""
        SELECT code AS 코드, name AS 이름,
               COALESCE(total_budget,0) AS 총예산,
               COALESCE(show_conversion,1) AS 전환표시,
               COALESCE(show_creative,0) AS 소재표시,
               created_at AS 생성일
        FROM advertisers ORDER BY created_at DESC
    """, engine)

    st.subheader(f"등록된 광고주 ({len(advs)}개)")
    advs_show = advs.copy()
    advs_show["총예산"]   = advs_show["총예산"].apply(lambda x: f"₩{x:,.0f}")
    advs_show["전환표시"] = advs_show["전환표시"].apply(lambda x: "✅ 표시" if x else "❌ 숨김")
    advs_show["소재표시"] = advs_show["소재표시"].apply(lambda x: "✅ 표시" if x else "❌ 숨김")
    st.dataframe(advs_show, use_container_width=True, hide_index=True)
    st.divider()

    st.subheader("➕ 광고주 추가")
    with st.form("add_adv"):
        c1, c2, c3 = st.columns(3)
        with c1: new_code   = st.text_input("코드 (예: GAME_C)")
        with c2: new_name   = st.text_input("이름")
        with c3: new_budget = st.number_input("총 예산 (₩)", min_value=0, step=100000, value=0)
        cc1, cc2 = st.columns(2)
        with cc1: new_show_conv = st.checkbox("전환지표 표시", value=True)
        with cc2: new_show_cre  = st.checkbox("광고 소재 분석 탭 표시", value=False)
        if st.form_submit_button("추가", type="primary"):
            if not new_code or not new_name:
                st.error("코드와 이름을 입력하세요")
            else:
                try:
                    adv_code_clean = new_code.strip().upper()
                    q("""INSERT INTO advertisers (code, name, total_budget, show_conversion, show_creative)
                         VALUES (?,?,?,?,?)""",
                      (adv_code_clean, new_name.strip(), float(new_budget),
                       1 if new_show_conv else 0, 1 if new_show_cre else 0), fetch=False)
                    q("INSERT INTO permissions (email, advertiser_code, level) VALUES (?,?,?)",
                      (user["email"], adv_code_clean, "OWNER"), fetch=False)
                    email_v, pw_v = create_viewer_account(adv_code_clean, new_name)
                    st.success(f"{new_name} 추가 완료\n뷰어 계정: {email_v} / PW: {pw_v}")
                    st.rerun()
                except Exception as e:
                    st.error(f"오류: {e}")

    st.divider()
    st.subheader("✏️ 광고주 편집")
    if not advs.empty:
        edit_code = st.selectbox("편집할 광고주", advs["코드"].tolist(),
            format_func=lambda c: f"{c} — {advs[advs['코드']==c]['이름'].iloc[0]}")
        cur_row = advs[advs["코드"] == edit_code].iloc[0]
        with st.form("edit_adv_form"):
            c1, c2 = st.columns(2)
            with c1: new_name2   = st.text_input("이름", value=cur_row["이름"])
            with c2: new_budget2 = st.number_input("총 예산 (₩)", min_value=0, step=100000,
                                                    value=int(cur_row["총예산"]))
            cc1, cc2 = st.columns(2)
            with cc1: new_show2     = st.checkbox("전환지표 표시",     value=bool(cur_row["전환표시"]))
            with cc2: new_show_cre2 = st.checkbox("광고 소재 탭 표시", value=bool(cur_row["소재표시"]))
            if st.form_submit_button("💾 변경 저장", type="primary"):
                q("""UPDATE advertisers SET name=?, total_budget=?,
                     show_conversion=?, show_creative=? WHERE code=?""",
                  (new_name2, float(new_budget2),
                   1 if new_show2 else 0, 1 if new_show_cre2 else 0, edit_code), fetch=False)
                st.success("변경됨"); st.rerun()

    st.divider()
    st.subheader("🗑️ 광고주 삭제")
    if not advs.empty:
        del_code = st.selectbox("삭제할 광고주", advs["코드"].tolist(), key="del_sel",
            format_func=lambda c: f"{c} — {advs[advs['코드']==c]['이름'].iloc[0]}")
        with st.form("del_adv_form"):
            confirm = st.text_input(f"코드 '{del_code}' 를 입력하세요")
            if st.form_submit_button("🗑️ 영구 삭제", type="primary"):
                if confirm == del_code:
                    for tbl in ["perf","upload_log","conversion_mapping","permissions",
                                "funnel_mapping","creative_images","api_credentials","api_sync_log"]:
                        q(f"DELETE FROM {tbl} WHERE advertiser_code=?", (del_code,), fetch=False)
                    q("DELETE FROM advertisers WHERE code=?", (del_code,), fetch=False)
                    st.success("삭제됨"); st.rerun()
                else:
                    st.error("코드 불일치")


# ============ 계정 관리 ============
elif page == "👤 계정 관리":
    st.title("👤 계정 관리")
    if not is_admin:
        st.error("관리자만 접근 가능합니다"); st.stop()

    users_df = pd.read_sql("""
        SELECT u.email, u.name, u.role,
               STRING_AGG(p.advertiser_code, ',') AS advertisers
        FROM users u
        LEFT JOIN permissions p ON u.email = p.email
        GROUP BY u.email, u.name, u.role
        ORDER BY u.email
    """, engine)

    st.subheader("📋 계정 목록")
    st.dataframe(users_df, use_container_width=True, hide_index=True)
    st.divider()

    adv_list    = q("SELECT code FROM advertisers", fetch=True)
    adv_options = [a[0] for a in adv_list]

    st.subheader("➕ 계정 생성")
    with st.form("create_user_form"):
        new_email = st.text_input("이메일")
        new_name  = st.text_input("이름")
        new_pw    = st.text_input("비밀번호", type="password")
        new_role  = st.selectbox("권한", ["AGENCY_ADMIN","OWNER","MANAGER","VIEWER"])
        sel_advs  = st.multiselect("광고주 연결", adv_options)
        if st.form_submit_button("✅ 계정 생성", type="primary"):
            if not new_email or not new_pw:
                st.error("이메일/비밀번호 입력 필요")
            else:
                try:
                    q("INSERT INTO users (email, name, role, password) VALUES (?,?,?,?)",
                      (new_email, new_name, new_role, new_pw), fetch=False)
                    for adv in sel_advs:
                        q("INSERT INTO permissions (email, advertiser_code, level) VALUES (?,?,?)",
                          (new_email, adv, new_role), fetch=False)
                    st.success("계정 생성 완료"); st.rerun()
                except Exception as e:
                    st.error(f"오류: {e}")

    st.divider()
    st.subheader("✏️ 계정 수정")
    sel_user = st.selectbox("계정 선택", users_df["email"], key="select_user")
    urow = users_df[users_df["email"] == sel_user].iloc[0]
    with st.form("edit_user_form"):
        edit_name = st.text_input("이름", value=urow["name"])
        roles     = ["AGENCY_ADMIN","OWNER","MANAGER","VIEWER"]
        edit_role = st.selectbox("권한", roles,
            index=roles.index(urow["role"]) if urow["role"] in roles else 0)
        new_pw2   = st.text_input("새 비밀번호 (변경 시에만)", type="password")
        cur_advs  = urow["advertisers"].split(",") if urow["advertisers"] else []
        edit_advs = st.multiselect("광고주", adv_options,
                                   default=[a for a in cur_advs if a in adv_options])
        if st.form_submit_button("💾 수정 저장", type="primary"):
            q("UPDATE users SET name=?, role=? WHERE email=?",
              (edit_name, edit_role, sel_user), fetch=False)
            if new_pw2:
                q("UPDATE users SET password=? WHERE email=?", (new_pw2, sel_user), fetch=False)
            q("DELETE FROM permissions WHERE email=?", (sel_user,), fetch=False)
            for adv in edit_advs:
                q("INSERT INTO permissions (email, advertiser_code, level) VALUES (?,?,?)",
                  (sel_user, adv, edit_role), fetch=False)
            st.success("수정 완료"); st.rerun()

    st.divider()
    st.subheader("🗑️ 계정 삭제")
    with st.form("del_user_form"):
        del_user = st.selectbox("삭제할 계정", users_df["email"])
        if st.form_submit_button("🗑️ 삭제 확인", type="primary"):
            q("DELETE FROM permissions WHERE email=?", (del_user,), fetch=False)
            q("DELETE FROM users WHERE email=?",       (del_user,), fetch=False)
            st.success("삭제 완료"); st.rerun()
