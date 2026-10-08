"""
Debt Risk Radar.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import html
import os
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from catalog import (
    ACTIVE_SOURCE_HOSTS, BUCKET_LABELS, CURRENT_STRESS_BUCKETS, METHODOLOGY_VERSION,
    WATCH_LEVEL, STRESS_LEVEL, STRUCTURAL_BUCKETS,
)
from http_cache import collection_status
from data import (
    bis_credit_metrics,
    bucket_scores,
    build_debt_dynamics_projection,
    cbo_projection_metrics,
    combine_metrics,
    fetch_bis_credit,
    fetch_cbo_projections,
    fetch_fred_series,
    fetch_treasury_debt,
    fetch_world_bank,
    fred_metrics,
    current_stress_score,
    score_color,
    score_label,
    score_coverage,
    treasury_daily_metrics,
    world_bank_metrics,
)


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(value, minimum)
    return value


AUTO_REFRESH_SECONDS = env_int("DEBT_RISK_RADAR_AUTO_REFRESH_SECONDS", 15 * 60, minimum=60)
AUTO_REFRESH_PARAM = "_drr_auto_refresh"
VIEW_PARAM = "view"
DEFAULT_COUNTRY = "USA"
DEFAULT_FRED_START = "1990-01-01"
DEFAULT_TREASURY_START = "2015-01-01"
QUALITY_LABELS = {"ok": "Disponible", "projection": "Projection", "missing": "Absent",
                  "stale": "Trop ancien", "invalid": "Invalide", "unscored": "Historique insuffisant"}


def query_value(name: str, default: str = "") -> str:
    value = st.query_params.get(name, default)
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value)


def current_view() -> str:
    view = query_value(VIEW_PARAM, "radar").lower()
    return "faq" if view in {"faq", "help", "aide"} else "radar"


def install_auto_refresh(seconds: int = AUTO_REFRESH_SECONDS) -> None:
    st.html(
        f"""
        <script>
        window.setTimeout(function () {{
            const url = new URL(window.parent.location.href);
            url.searchParams.set("{AUTO_REFRESH_PARAM}", Date.now().toString());
            window.parent.location.replace(url.toString());
        }}, {seconds * 1000});
        </script>
        """,
        unsafe_allow_javascript=True,
    )


st.set_page_config(
    page_title="Debt Risk Radar",
    page_icon=":bar_chart:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
    :root {
        --bg: #1a1a1a;
        --panel: #202020;
        --panel2: #171717;
        --line: rgba(0, 240, 208, 0.16);
        --line-soft: rgba(255, 255, 255, 0.10);
        --text: #b8fff5;
        --paper: #e7e9ee;
        --bright: #f5f6f8;
        --dim: #6f9b94;
        --faint: #456b65;
        --usd: #00f0d0;
        --teal: #5eead4;
        --yen: #ff6b9d;
        --rose: #ff4d87;
        --gold: #f5b13d;
        --orange: #ff8a3d;
        --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    }
    .stApp {
        background: radial-gradient(1100px 520px at 50% -8%, rgba(94, 234, 212, 0.05), transparent 62%), var(--bg);
        color: var(--text);
        font-family: var(--mono);
    }
    .stApp::after {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        z-index: 9999;
        background: repeating-linear-gradient(0deg, rgba(0,0,0,0) 0, rgba(0,0,0,0) 2px, rgba(0,0,0,.08) 3px);
        mix-blend-mode: multiply;
        opacity: .42;
    }
    .block-container { padding-top: 1.2rem; max-width: 1220px; }
    h1, h2, h3 {
        color: var(--bright);
        letter-spacing: 0;
        font-family: var(--mono);
    }
    h1 {
        border-bottom: 1px solid var(--line);
        padding-bottom: 0.45rem;
        text-shadow: 0 0 30px rgba(94, 234, 212, 0.18);
    }
    h2 {
        color: var(--dim);
        font-size: 0.92rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }
    h2::before {
        content: "";
        display: inline-block;
        width: 18px;
        height: 2px;
        margin-right: 10px;
        vertical-align: middle;
        background: var(--yen);
    }
    div[data-testid="stSidebar"] {
        background: #171717;
        border-right: 1px solid var(--line);
    }
    div[data-testid="stSidebar"] label, div[data-testid="stSidebar"] p {
        color: var(--dim);
        font-family: var(--mono);
    }
    .tape {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 12px 22px;
        border-bottom: 1px solid var(--line);
        padding-bottom: 14px;
        margin-bottom: 20px;
    }
    .brand {
        color: var(--bright);
        font-size: 1.15rem;
        font-weight: 800;
    }
    .brand b { color: var(--yen); }
    .tagline {
        color: var(--dim);
        font-size: 0.72rem;
        letter-spacing: 0.03em;
    }
    .status-pill {
        margin-left: auto;
        color: var(--dim);
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 4px 10px;
        font-size: 0.7rem;
        display: flex;
        gap: 8px;
        align-items: center;
    }
    .status-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: var(--usd);
        box-shadow: 0 0 0 0 rgba(0,240,208,.55);
    }
    .top-nav {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-left: auto;
        align-items: center;
    }
    .nav-link {
        color: var(--dim) !important;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 4px 10px;
        font-size: 0.7rem;
        text-decoration: none !important;
    }
    .nav-link:hover, .nav-link.active {
        color: var(--bright) !important;
        border-color: rgba(94, 234, 212, 0.65);
        background: rgba(94, 234, 212, 0.06);
    }
    .kpi-grid {
        display: grid;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 1px;
        background: var(--line);
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
        margin: 18px 0 16px;
    }
    .metric-card {
        background: var(--panel);
        border: 0;
        border-left: 3px solid var(--usd);
        border-radius: 0;
        padding: 15px 15px 13px;
        min-height: 112px;
    }
    .metric-label {
        color: var(--dim);
        text-transform: uppercase;
        font-size: 0.66rem;
        letter-spacing: 0.07em;
        margin-bottom: 4px;
    }
    .metric-value {
        font-family: var(--mono);
        font-weight: 700;
        font-size: 1.85rem;
        line-height: 1.15;
        font-variant-numeric: tabular-nums;
    }
    .metric-sub {
        color: var(--dim);
        font-size: 0.76rem;
        margin-top: 8px;
        line-height: 1.35;
    }
    .help-card {
        background: rgba(255, 255, 255, 0.018);
        border: 1px solid var(--line);
        border-left: 2px solid var(--teal);
        border-radius: 10px;
        padding: 13px 15px;
        color: var(--dim);
        font-size: 0.82rem;
        line-height: 1.55;
        margin: 8px 0 14px;
    }
    .help-card strong { color: var(--paper); }
    .faq-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
        margin: 10px 0 18px;
    }
    .faq-card {
        background: rgba(255, 255, 255, 0.018);
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 14px 15px;
        min-height: 128px;
        color: var(--dim);
        font-size: 0.82rem;
        line-height: 1.52;
    }
    .faq-card strong {
        color: var(--paper);
        display: block;
        margin-bottom: 6px;
    }
    .compact-wrap {
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
        background: var(--panel);
    }
    .detail-item {
        border-bottom: 1px solid var(--line-soft);
        padding: 9px 2px;
        color: var(--dim);
        font-size: 0.82rem;
        line-height: 1.45;
    }
    .detail-item strong { color: var(--paper); }
    .detail-meta {
        color: var(--faint);
        font-size: 0.72rem;
        margin-top: 4px;
    }
    .risk-table {
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
        background: var(--panel2);
        margin-top: 14px;
    }
    .risk-row {
        display: grid;
        grid-template-columns: minmax(132px, .95fr) minmax(260px, 2.05fr) minmax(96px, .55fr) minmax(84px, .45fr) minmax(126px, .7fr);
        border-top: 1px solid var(--line-soft);
        align-items: stretch;
    }
    .risk-row:first-child { border-top: 0; }
    .risk-head {
        background: rgba(122, 162, 247, 0.08);
        color: var(--dim);
        text-transform: uppercase;
        font-size: 0.68rem;
        letter-spacing: 0.07em;
    }
    .risk-cell {
        min-width: 0;
        padding: 10px 12px;
        border-left: 1px solid var(--line-soft);
        overflow-wrap: anywhere;
        word-break: normal;
        color: var(--text);
        font-size: 0.84rem;
        line-height: 1.35;
        font-variant-numeric: tabular-nums;
    }
    .risk-cell:first-child { border-left: 0; }
    .risk-family { color: var(--paper); font-weight: 700; }
    .risk-muted { color: var(--dim); }
    .risk-score {
        display: grid;
        grid-template-columns: 1fr auto;
        align-items: center;
        gap: 10px;
    }
    .risk-bar {
        height: 8px;
        border-radius: 999px;
        overflow: hidden;
        background: rgba(255, 255, 255, 0.10);
    }
    .risk-bar span {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: var(--teal);
        box-shadow: 0 0 12px rgba(94, 234, 212, 0.3);
    }
    div[data-testid="stDataFrame"] {
        border: 1px solid var(--line);
        border-radius: 10px;
        overflow: hidden;
    }
    div[data-testid="stDataFrame"] * {
        font-family: var(--mono) !important;
    }
    .stAlert {
        border: 1px solid var(--line);
        border-radius: 10px;
    }
    .site-footer {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-top: 34px;
        padding: 18px 0 6px;
        border-top: 1px solid var(--line);
        color: var(--faint);
        font-size: 0.72rem;
    }
    .l0g-logo {
        display: inline-flex;
        align-items: baseline;
        gap: 2px;
        color: var(--bright) !important;
        font-weight: 800;
        text-decoration: none !important;
        letter-spacing: 0;
    }
    .l0g-logo .slash {
        color: var(--teal);
    }
    .l0g-logo:hover {
        color: var(--teal) !important;
    }
    footer { visibility: hidden; }
    #MainMenu { visibility: hidden; }
    @media (max-width: 1180px) {
        .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .faq-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
        .kpi-grid { grid-template-columns: 1fr; }
        .status-pill { margin-left: 0; }
        .risk-head { display: none; }
        .risk-row {
            grid-template-columns: 1fr;
            padding: 8px 0;
        }
        .risk-cell {
            border-left: 0;
            display: grid;
            grid-template-columns: 94px minmax(0, 1fr);
            gap: 10px;
            padding: 5px 12px;
        }
        .risk-cell::before {
            content: attr(data-label);
            color: var(--dim);
            text-transform: uppercase;
            font-size: 0.66rem;
            letter-spacing: 0.06em;
        }
    }
</style>
""",
    unsafe_allow_html=True,
)


def metric_card(label: str, value: str, sub: str, color: str) -> str:
    label = html.escape(label)
    value = html.escape(value)
    sub = html.escape(sub)
    color = html.escape(color)
    return f"""
    <div class="metric-card" style="border-left-color: {color};">
        <div class="metric-label">{label}</div>
        <div class="metric-value" style="color: {color};">{value}</div>
        <div class="metric-sub">{sub}</div>
    </div>
    """


def format_number(value: float, unit: str = "") -> str:
    if pd.isna(value):
        return "N/D"
    if abs(value) >= 1000:
        return f"{value:,.0f} {unit}".strip()
    return f"{value:,.2f} {unit}".strip()


def risk_table_html(table: pd.DataFrame, limit: int = 16) -> str:
    rows = [
        '<div class="risk-row risk-head">'
        '<div class="risk-cell">famille</div>'
        '<div class="risk-cell">signal</div>'
        '<div class="risk-cell">valeur</div>'
        '<div class="risk-cell">unite</div>'
        '<div class="risk-cell">risque</div>'
        "</div>"
    ]
    for _, row in table.head(limit).iterrows():
        risk_score = float(row["risk_score"])
        width = max(0.0, min(100.0, risk_score)) if pd.notna(risk_score) else 0.0
        score_text = f"{risk_score:.0f}" if pd.notna(risk_score) else "N/D"
        rows.append(
            '<div class="risk-row">'
            f'<div class="risk-cell risk-family" data-label="famille">{html.escape(str(row["famille"]))}</div>'
            '<div class="risk-cell" data-label="signal">'
            f'{html.escape(str(row["name"]))}'
            f'<div class="detail-meta">{html.escape(str(row["source"]))} · {html.escape(str(row["date"]))}</div>'
            f'<div class="detail-meta">{QUALITY_LABELS.get(row.get("quality"), "Qualité inconnue")}</div>'
            "</div>"
            f'<div class="risk-cell" data-label="valeur">{format_number(float(row["current"]))}</div>'
            f'<div class="risk-cell risk-muted" data-label="unite">{html.escape(str(row["unit"]))}</div>'
            '<div class="risk-cell" data-label="risque">'
            '<div class="risk-score">'
            f'<div class="risk-bar"><span style="width:{width:.0f}%"></span></div>'
            f"<strong>{score_text}</strong>"
            "</div>"
            "</div>"
            "</div>"
        )
    return f'<div class="risk-table">{"".join(rows)}</div>'


def chart_layout(fig: go.Figure, title: str, height: int = 360, yaxis_title: str | None = None) -> go.Figure:
    fig.update_layout(
        title=title,
        height=height,
        margin=dict(l=10, r=10, t=42, b=10),
        paper_bgcolor="#1a1a1a",
        plot_bgcolor="#1a1a1a",
        font=dict(color="#b8fff5", family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"),
        legend=dict(orientation="h"),
        xaxis=dict(gridcolor="rgba(0,240,208,0.08)", zerolinecolor="rgba(0,240,208,0.18)"),
        yaxis=dict(gridcolor="rgba(0,240,208,0.08)", zerolinecolor="rgba(0,240,208,0.18)"),
    )
    if yaxis_title:
        fig.update_yaxes(title_text=yaxis_title)
    return fig


def render_header(view: str) -> None:
    radar_active = "active" if view == "radar" else ""
    faq_active = "active" if view == "faq" else ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    st.markdown(
        f"""
        <header class="tape">
            <div class="brand"><b>Debt</b> Risk <b>Radar</b></div>
            <div class="tagline">USA · dette souveraine · credit gap · stress de marche</div>
            <nav class="top-nav" aria-label="Navigation">
                <a class="nav-link {radar_active}" href="?">Radar</a>
                <a class="nav-link {faq_active}" href="?view=faq">Aide / FAQ</a>
            </nav>
            <div class="status-pill"><span class="status-dot"></span>{timestamp}</div>
        </header>
        """,
        unsafe_allow_html=True,
    )


def render_site_footer() -> None:
    st.markdown(
        """
        <div class="site-footer">
          <a class="l0g-logo" href="https://l0g.fr" target="_blank" rel="noopener" aria-label="l0g.fr">
            <span class="slash">//</span><span>l0g.fr</span>
          </a>
          <span>Debt Risk Radar · MIT License</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_faq_page() -> None:
    st.markdown("## Aide / FAQ")
    st.markdown(
        f"""
        <div class="help-card">
          <strong>Objectif.</strong> Debt Risk Radar est un tableau de bord de surveillance du risque de dette.
          Il ne prédit pas une crise et ne produit pas de signal d'achat ou de vente : il indique quels canaux
          méritent d'être lus en premier quand la dette, les taux, le crédit ou les spreads se tendent.
        </div>
        <div class="faq-grid">
          <div class="faq-card"><strong>Périmètre</strong> Le radar est centré sur la dette US : Treasury, CBO, FRED, BIS et World Bank sont lus sur un socle américain fixe ({DEFAULT_COUNTRY}).</div>
          <div class="faq-card"><strong>Score courant 0-100</strong> 50 signale une zone élevée, 65 une surveillance active, 80 un stress. Ce score exclut les projections CBO long terme.</div>
          <div class="faq-card"><strong>Méthode {METHODOLOGY_VERSION}</strong> Treasury Fiscal Data, FRED, BIS et World Bank fournissent 31 signaux courants. Les 4 projections CBO sont séparées. Aucun prix d'ETF n'est collecté.</div>
          <div class="faq-card"><strong>Lecture</strong> Le score courant exclut les projections CBO long terme. Elles restent visibles comme signal structurel, mais ne pilotent pas le stress de marché.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("Que mesure exactement le radar ?", expanded=True):
        st.markdown(
            """
            Le radar suit plusieurs familles de signaux courants qui peuvent se renforcer :
            dette publique, charge d'intérêts, déficit, crédit privé,
            conditions de marché, liquidité et indicateurs annuels américains.

            Les projections CBO long terme sont conservées comme couche structurelle séparée :
            elles renseignent la soutenabilité, mais ne décrivent pas un stress de marché immédiat.

            L'idée n'est pas de ramener toute la dette mondiale à un seul chiffre parfait,
            mais de repérer rapidement le canal qui se détériore.
            """
        )

    with st.expander("Quel est le périmètre géographique ?"):
        st.markdown(
            f"""
            Le dashboard est volontairement centré sur la dette US. Le périmètre est fixé :
            - `{DEFAULT_COUNTRY}` pour Treasury, CBO, FRED, BIS et World Bank.
            - Taux et spreads du marché américain via FRED.

            Les métriques de marché servent de proxies de transmission : elles indiquent si les taux,
            ou les spreads crédit commencent à refléter une prime de risque.
            """
        )

    with st.expander("Comment est construit le score de risque ?"):
        st.markdown(
            """
            Chaque série est transformée en signal comparable quand l'historique le permet :
            niveau courant, écart à son régime récent, sens du risque, puis normalisation de 0 à 100.

            Les familles courantes sont ensuite agrégées avec des pondérations explicites.
            Le bucket `cbo_projection` est exclu de ce score courant parce qu'il repose sur des valeurs
            terminales de projection longue. Il reste affiché comme score structurel séparé.

            Si un signal courant attendu manque, est trop ancien ou ne peut pas être calculé,
            le score global devient indisponible. Aucun score neutre n'est inventé et les poids
            des autres familles ne sont pas augmentés. Les signaux valides restent consultables.
            """
        )

    with st.expander("Comment lire la carte de risque ?"):
        st.markdown(
            """
            Le graphique classe les grandes familles de risque et marque explicitement le bucket CBO
            comme `structurel`. Le tableau sous le graphique détaille les signaux individuels : famille,
            nom du signal, valeur, unité, source, date et score.

            Les signaux courants sont listés avant les signaux structurels. Une famille élevée n'est
            pas forcément une crise : c'est un pointeur. Il faut ouvrir les sources et regarder si le
            signal vient d'une tendance lente, d'une projection longue ou d'un choc récent.
            """
        )

    with st.expander("Pourquoi certains flux peuvent manquer ?"):
        st.markdown(
            """
            Certaines sources sont gratuites et sans clé, comme Treasury, BIS, CBO ou World Bank.
            FRED nécessite une clé serveur gratuite. Ses 19 signaux sont requis pour le score courant.

            Un flux absent, en erreur ou trop ancien est signalé dans la couverture. Sa valeur
            ne remplace jamais une observation récente. Le score courant est suspendu tant que
            les signaux attendus ne sont pas tous disponibles.

            Un quota dépassé chez un fournisseur suspend temporairement ses appels. La collecte
            planifiée reprend après la pause, qui s'allonge si les refus se répètent. Pendant
            cette attente, les signaux valides restent visibles. Recharger la page ne force
            aucun appel et ne raccourcit pas la pause.

            Si un renouvellement échoue, la dernière réponse reste utilisable jusqu'à son
            échéance initiale. La pause du fournisseur et l'heure de reprise autorisée sont
            signalées séparément. À l'expiration, le signal devient indisponible et le score
            courant est suspendu si sa couverture est incomplète.
            """
        )

    with st.expander("À quelle fréquence les données se rafraîchissent-elles ?"):
        st.markdown(
            f"""
            L'app se rafraîchit automatiquement toutes les `{AUTO_REFRESH_SECONDS // 60}` minutes.
            Le cache est valide six heures pour Treasury et FRED, et vingt-quatre heures
            pour BIS, CBO et World Bank. Le collecteur commence son renouvellement jusqu'à trente
            minutes avant l'expiration afin d'éviter des interruptions entre deux passages.
            Une visite ou un rafraîchissement de la page publique ne lance aucune collecte.

            Attention : beaucoup de séries publiques sont trimestrielles, annuelles ou publiées avec délai.
            Le rafraîchissement de l'app ne transforme pas une série lente en donnée temps réel.
            """
        )

    with st.expander("Comment lire une qualité dégradée ou une date ancienne ?"):
        st.markdown(
            "La qualité décrit la disponibilité des données, indépendamment du niveau de risque économique. "
            "Les contrôles portent sur chaque signal : présence, valeur finie, date, historique et fraîcheur. "
            "Les seuils de tolérance sont de 10 jours pour les observations quotidiennes, 28 pour les "
            "hebdomadaires, 280 pour les séries FRED trimestrielles datées en début de période et 300 "
            "pour BIS daté en fin de trimestre. Les séries annuelles disposent de 900 jours en fin "
            "de période, ou 1 100 en début de période. Ce sont des tolérances de surveillance, "
            "pas une garantie de disposer de la dernière publication. Les périodes ne sont pas "
            "les dates de publication. Le CBO utilise le millésime de février 2026 : 2056 est "
            "un horizon de projection, jamais une date de fraîcheur."
        )

    with st.expander("Pourquoi la méthode 2.0 ne contient-elle plus d'ETF ?"):
        st.markdown(
            "La méthode 2.0 retire les cinq prix TLT, HYG, LQD, SHY et SPY, leurs trois ratios "
            "et la volatilité HYG. Le dashboard et le collecteur n'appellent plus Massive. "
            "Les taux et spreads FRED restent leurs propres séries ; ils ne remplacent pas ces prix.\n\n"
            "Les sept familles courantes conservent leurs poids relatifs : fiscal 22/86, taux et crédit "
            "18/86, dette privée 12/86, liquidité 10/86, Treasury 10/86, World Bank 4/86 et BIS 10/86. "
            "Ce changement de périmètre rompt la comparabilité avec l'ancien score. Un écart lors "
            "de la bascule ne doit pas être interprété comme une variation du risque.\n\n"
            "Le score exige toujours les 31 signaux courants valides. Une panne institutionnelle "
            "peut encore le suspendre ; aucune donnée expirée n'est prolongée."
        )

    with st.expander("L'accès gratuit autorise-t-il toute réutilisation ?"):
        st.markdown(
            "Les API actives ne nécessitent pas d'abonnement payant pour la collecte configurée. "
            "Cela ne constitue pas une licence générale de redistribution : certaines séries FRED, "
            "dont les spreads ICE BofA, relèvent de droits tiers. Vérifier les conditions de chaque "
            "série avant une réutilisation commerciale. "
            "[Conditions FRED](https://fred.stlouisfed.org/docs/api/terms_of_use.html)."
        )

    with st.expander("Quelles sont les limites importantes ?"):
        st.markdown(
            """
            Le radar ne remplace pas une analyse pays, devise, maturité, détenteurs de dette,
            liquidité de marché ou soutenabilité budgétaire complète.

            Le score courant n'est pas comparable mécaniquement à d'autres dashboards l0g. Il sert à
            ordonner la lecture du stress de dette dans cette app, pas à prédire une date de défaut,
            de downgrade ou de crise.
            """
        )

    with st.expander("Comment vérifier les sources utilisées ?"):
        st.markdown(
            """
            La section `Audit sources` en bas du radar liste les fournisseurs effectivement utilisés,
            le nombre de métriques attendues et utilisables, ainsi que les dates d'observation.

            C'est le bon endroit pour vérifier rapidement si le score repose sur toutes les familles
            attendues. Les 31 signaux courants sont requis ; les 4 projections CBO sont auditées séparément.
            Le JSON public `latest.json` identifie la méthode et ses pondérations.
            """
        )


country = DEFAULT_COUNTRY
fred_start = DEFAULT_FRED_START
treasury_start = DEFAULT_TREASURY_START

view = current_view()
render_header(view)

if view == "faq":
    render_faq_page()
    render_site_footer()
    st.stop()

install_auto_refresh()

st.markdown(
    f"""
    <div class="help-card">
      <strong>Méthode {METHODOLOGY_VERSION} · socle institutionnel US.</strong> Le radar suit 31 signaux courants
      via Treasury, FRED, BIS et World Bank ({country}), plus 4 projections CBO séparées.
      Les ETF sont exclus ; ce score n'est pas directement comparable à l'ancienne méthode. Auto-refresh toutes les
      {AUTO_REFRESH_SECONDS // 60} min.
    </div>
    """,
    unsafe_allow_html=True,
)

issues = []

with st.spinner("Chargement Treasury, FRED, BIS, CBO et World Bank..."):
    treasury_df, treasury_issues = fetch_treasury_debt(str(treasury_start))
    fred_data, fred_issues = fetch_fred_series(str(fred_start))
    wb_df, wb_issues = fetch_world_bank(country)
    bis_df, bis_issues = fetch_bis_credit(country)
    cbo_df, cbo_issues = fetch_cbo_projections()
    issues.extend(treasury_issues + fred_issues + wb_issues + bis_issues + cbo_issues)

treasury_metrics_df = treasury_daily_metrics(treasury_df)
fred_metrics_df = fred_metrics(fred_data)
wb_metrics_df = world_bank_metrics(wb_df)
bis_metrics_df = bis_credit_metrics(bis_df)
cbo_metrics_df = cbo_projection_metrics(cbo_df)
metrics = combine_metrics(
    treasury_metrics_df,
    fred_metrics_df,
    wb_metrics_df,
    bis_metrics_df,
    cbo_metrics_df,
)
buckets = bucket_scores(metrics)
gscore = current_stress_score(buckets)
coverage = score_coverage(buckets, expected_buckets=CURRENT_STRESS_BUCKETS)

cbo_score = buckets.loc[buckets["bucket"] == "cbo_projection", "score"]
fiscal_score = buckets.loc[buckets["bucket"] == "fiscal", "score"]
fiscal_value = float(fiscal_score.iloc[0]) if len(fiscal_score) else np.nan
market_score = buckets.loc[buckets["bucket"] == "rates_market", "score"]
market_value = float(market_score.iloc[0]) if len(market_score) else np.nan
private_score = buckets.loc[buckets["bucket"] == "global_credit", "score"]
private_value = float(private_score.iloc[0]) if len(private_score) else np.nan
cbo_value = float(cbo_score.iloc[0]) if len(cbo_score) else np.nan
st.markdown(
    f"""
    <div class="kpi-grid">
        {metric_card("Stress courant", format_number(gscore), score_label(gscore), score_color(gscore))}
        {metric_card("Fiscal courant", format_number(fiscal_value), score_label(fiscal_value), score_color(fiscal_value))}
        {metric_card("Taux / crédit", format_number(market_value), score_label(market_value), score_color(market_value))}
        {metric_card("Credit BIS", format_number(private_value), score_label(private_value), score_color(private_value))}
        {metric_card("CBO structurel", format_number(cbo_value), score_label(cbo_value), score_color(cbo_value))}
    </div>
    """,
    unsafe_allow_html=True,
)

st.caption(f"Couverture des signaux courants : {coverage:.1%}. Les dates affichées sont les périodes d'observation.")
collection = collection_status(ACTIVE_SOURCE_HOSTS)
if collection["status"] == "paused":
    st.info("Le renouvellement de certaines sources est temporairement en pause. "
            "Les réponses encore valides restent utilisables jusqu'à leur échéance initiale.")
    with st.expander("État du renouvellement", expanded=False):
        reasons = {"rate_limit": "quota du fournisseur", "authorization": "accès refusé",
                   "http_error": "erreur du fournisseur", "network_error": "échec de connexion",
                   "invalid_response": "réponse rejetée", "upstream_failure": "échec du fournisseur"}
        for provider in collection["providers"]:
            st.write(f"{provider['source']} : {reasons[provider['reason']]}. "
                     f"Nouvel essai autorisé à partir de {provider['retry_at']} (UTC), "
                     "lors d'un passage planifié.")
unavailable = metrics[~metrics["eligible"]]
if not unavailable.empty or issues:
    st.warning("Qualité des données dégradée. Les signaux indisponibles ne contribuent pas au score ; le score courant est suspendu si sa couverture est incomplète.")
    with st.expander("Signaux à vérifier", expanded=False):
        for _, row in unavailable.iterrows():
            observed_date = row["date"].strftime("%Y-%m-%d") if pd.notna(row["date"]) else "N/D"
            st.write(f"{row['name']} : {QUALITY_LABELS[row['quality']]} ; observation : {observed_date}.")

st.markdown(
    """
    <div class="help-card">
      <strong>Lecture rapide.</strong> Le score courant 0-100 agrège des signaux de dette publique,
      de credit gap, de cout des interets, de liquidite et de marche. Les projections CBO long terme
      sont affichees comme risque structurel, mais exclues du score courant parce qu'elles ne mesurent
      pas un choc de marche actuel. 50 marque une zone elevee, 65 une surveillance active, 80 un stress.
      Une couverture incomplete suspend le score global ; N/D ne signifie jamais un risque nul.
    </div>
    """,
    unsafe_allow_html=True,
)

if issues:
    with st.expander(
        f"Disponibilité des sources ({len(issues)})",
        expanded=False,
    ):
        for issue in issues:
            st.write(f"{issue.source}: {issue.detail}")

st.markdown("## Carte de risque")
st.markdown(
    """
    <div class="help-card">
      <strong>Score courant vs structurel.</strong> Les buckets courants alimentent le score de stress.
      Le bucket CBO reste visible pour la soutenabilite longue, mais il est marque structurel et exclu
      du score courant.
    </div>
    """,
    unsafe_allow_html=True,
)

if not metrics["eligible"].any():
    st.warning("Aucun signal utilisable. En attente d'une collecte valide.")
    render_site_footer()
    st.stop()

bucket_view = buckets.copy()
bucket_view["role"] = np.where(bucket_view["bucket"].isin(STRUCTURAL_BUCKETS), "structurel", "courant")
bucket_view["bucket_label"] = bucket_view["bucket"].map(BUCKET_LABELS).fillna(bucket_view["bucket"])
bucket_view.loc[bucket_view["role"] == "structurel", "bucket_label"] += " (structurel)"
bucket_view["bucket_label"] += " (" + bucket_view["n"].astype(str) + "/" + bucket_view["expected"].astype(str) + ")"
fig_bucket = go.Figure(
    go.Bar(
        x=round(bucket_view["score"], 1),
        y=bucket_view["bucket_label"],
        orientation="h",
        marker=dict(color=[score_color(v) for v in bucket_view["score"]]),
        text=[f"{v:.1f}" if pd.notna(v) else "N/D" for v in bucket_view["score"]],
        textposition="outside",
    )
)
fig_bucket.add_vline(x=WATCH_LEVEL, line_width=1, line_dash="dot", line_color="#f5b13d")
fig_bucket.add_vline(x=STRESS_LEVEL, line_width=1, line_dash="dot", line_color="#ff4d87")
fig_bucket.update_layout(
    height=390,
    xaxis=dict(range=[0, 100], title="Risk score"),
    yaxis=dict(autorange="reversed"),
    margin=dict(l=10, r=24, t=20, b=10),
    paper_bgcolor="#1a1a1a",
    plot_bgcolor="#1a1a1a",
    font=dict(color="#b8fff5"),
)
st.plotly_chart(fig_bucket, width="stretch")

table = metrics.copy()
table["role"] = np.where(table["bucket"].isin(STRUCTURAL_BUCKETS), "structurel", "courant")
table["role_order"] = np.where(table["role"] == "structurel", 1, 0)
table = table.sort_values(["role_order", "risk_score"], ascending=[True, False])
table["famille"] = table["bucket"].map(BUCKET_LABELS).fillna(table["bucket"])
table.loc[table["role"] == "structurel", "famille"] += " (structurel)"
table["date"] = table["date"].dt.strftime("%Y-%m-%d").fillna("N/D")
st.markdown(risk_table_html(table), unsafe_allow_html=True)
with st.expander("Lire les signaux et les sources", expanded=False):
    for _, row in table.head(12).iterrows():
        st.markdown(
            f"""
            <div class="detail-item">
              <strong>{html.escape(str(row['name']))}</strong> · score {format_number(row['risk_score'])}<br>
              {html.escape(str(row['rationale']))}
              <div class="detail-meta">{html.escape(str(row['source']))} · {html.escape(str(row['series_id']))} · {html.escape(str(row['date']))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.markdown("## Dette et marche")
st.markdown(
    """
    <div class="help-card">
      <strong>Ce bloc se lit comme un moniteur de transmission.</strong> La dette Treasury donne le stock a refinancer.
      Les taux Treasury et les spreads de crédit via FRED éclairent le coût de financement et les primes de risque.
      Ce sont des rendements et des écarts de rendement, pas des prix d'ETF.
    </div>
    """,
    unsafe_allow_html=True,
)

if not treasury_df.empty:
    daily = treasury_df.set_index("record_date").sort_index()
    fig_debt = go.Figure()
    fig_debt.add_trace(
        go.Scatter(
            x=daily.index,
            y=daily["tot_pub_debt_out_amt"] / 1_000_000_000_000,
            name="Total public debt",
            line=dict(color="#5eead4", width=2),
        )
    )
    fig_debt.add_trace(
        go.Scatter(
            x=daily.index,
            y=daily["debt_held_public_amt"] / 1_000_000_000_000,
            name="Held by public",
            line=dict(color="#f5b13d", width=2),
        )
    )
    chart_layout(fig_debt, "Dette US Treasury, trillions USD", height=430)
    st.plotly_chart(fig_debt, width="stretch")
else:
    st.info("Treasury daily debt feed unavailable.")

fig_market = go.Figure()
plotted = False
for series_id, name, color in (
    ("DGS10", "Treasury 10 ans (%)", "#f5b13d"),
    ("BAMLC0A0CM", "IG OAS (points de %)", "#5eead4"),
    ("BAMLH0A0HYM2", "HY OAS (points de %)", "#ff4d87"),
):
    if series_id in fred_data:
        fig_market.add_trace(go.Scatter(
            x=fred_data[series_id].index, y=fred_data[series_id], name=name,
            line=dict(color=color),
        ))
        plotted = True
chart_layout(fig_market, "Taux Treasury et spreads de crédit US · FRED", height=460)
fig_market.update_yaxes(title_text="Taux (%) / spread (points de %)")
if plotted:
    st.plotly_chart(fig_market, width="stretch")
else:
    st.info("Données de marché indisponibles.")

st.markdown("## Projections institutionnelles")
st.markdown(
    """
    <div class="help-card">
      <strong>Deux horizons de lecture.</strong> CBO donne une trajectoire budgetaire longue :
      c'est un signal structurel de soutenabilite, pas un choc courant. BIS mesure l'ecart du
      credit prive a sa tendance et reste dans la lecture de stress courant.
    </div>
    """,
    unsafe_allow_html=True,
)

inst_left, inst_right = st.columns(2)

with inst_left:
    if not cbo_df.empty:
        fig_cbo = go.Figure()
        for variable, name, color in [
            ("lt_debt_held_by_public_gdp_share", "Debt held by public / GDP", "#5eead4"),
            ("lt_outlays_net_interest_gdp_share", "Net interest / GDP", "#ff4d87"),
            ("lt_deficit_total_gdp_share", "Deficit / GDP", "#f5b13d"),
        ]:
            sub = cbo_df[cbo_df["variable"] == variable].sort_values("date")
            if sub.empty:
                continue
            fig_cbo.add_trace(go.Scatter(x=sub["date"], y=sub["value"], name=name, line=dict(color=color, width=2)))
        chart_layout(fig_cbo, "CBO long-term budget projections", yaxis_title="% GDP")
        st.plotly_chart(fig_cbo, width="stretch")
    else:
        st.info("CBO projections unavailable.")

with inst_right:
    fig_inst = go.Figure()
    plotted_inst = False
    if not bis_df.empty:
        gap = bis_df[bis_df["metric"] == "Credit-to-GDP gap"].sort_values("date")
        if not gap.empty:
            fig_inst.add_trace(
                go.Scatter(x=gap["date"], y=gap["value"], name=f"BIS credit gap {country}", line=dict(color="#5eead4"))
            )
            plotted_inst = True
    chart_layout(fig_inst, "BIS credit gap", yaxis_title="pp")
    if plotted_inst:
        st.plotly_chart(fig_inst, width="stretch")
    else:
        st.info("BIS feed unavailable.")

st.markdown("## Scenario dette / PIB")
st.markdown(
    """
    <div class="help-card">
      <strong>Regle de lecture.</strong> Si le taux effectif de la dette depasse durablement la croissance nominale
      et que le solde primaire reste negatif, la dette/PIB derive mecaniquement. Ce simulateur isole cette dynamique.
    </div>
    """,
    unsafe_allow_html=True,
)

default_debt = 120.0
st.caption("Scénario hypothétique : les paramètres sont des hypothèses, pas des prévisions institutionnelles.")
if "GFDEGDQ188S" in fred_data:
    default_debt = float(fred_data["GFDEGDQ188S"].dropna().iloc[-1])

scenario_cols = st.columns(5)
with scenario_cols[0]:
    initial_debt = st.number_input("Debt/GDP", min_value=0.0, max_value=300.0, value=round(default_debt, 1), step=1.0)
with scenario_cols[1]:
    primary_balance = st.number_input("Primary balance/GDP", min_value=-15.0, max_value=10.0, value=-3.0, step=0.25)
with scenario_cols[2]:
    nominal_growth = st.number_input("Nominal GDP growth", min_value=-5.0, max_value=15.0, value=3.8, step=0.25)
with scenario_cols[3]:
    effective_rate = st.number_input("Effective interest rate", min_value=0.0, max_value=15.0, value=4.0, step=0.25)
with scenario_cols[4]:
    horizon = st.slider("Years", min_value=3, max_value=30, value=10)

projection = build_debt_dynamics_projection(
    initial_debt_gdp=initial_debt,
    primary_balance_gdp=primary_balance,
    nominal_growth=nominal_growth,
    effective_rate=effective_rate,
    years=horizon,
)

fig_projection = go.Figure()
fig_projection.add_trace(
    go.Scatter(
        x=projection["year"],
        y=projection["debt_gdp"],
        name="Projected debt/GDP",
        line=dict(color="#5eead4", width=3),
        fill="tozeroy",
        fillcolor="rgba(94,234,212,0.08)",
    )
)
chart_layout(fig_projection, "Projection mecanique dette / PIB", height=380, yaxis_title="% GDP")
st.plotly_chart(fig_projection, width="stretch")

delta = projection["debt_gdp"].iloc[-1] - projection["debt_gdp"].iloc[0]
if delta > 10:
    st.warning(f"Debt ratio rises by {delta:.1f} points over {horizon} years. The scenario is not stabilizing.")
elif delta < -10:
    st.success(f"Debt ratio falls by {abs(delta):.1f} points over {horizon} years. The scenario is stabilizing.")
else:
    st.info(f"Debt ratio changes by {delta:.1f} points over {horizon} years. The scenario is broadly stable.")

st.markdown("## Sources et couverture")
st.markdown(
    """
    <div class="help-card">
      <strong>Lecture des sources.</strong> Les donnees institutionnelles viennent de Treasury Fiscal Data,
      BIS, CBO, World Bank et FRED. Les taux et spreads FRED restent distincts des prix d'ETF,
      retirés de la méthode 2.0. Les flux absents sont signalés plus haut.
      Une couverture courante incomplète suspend le score global.
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown("## Audit sources")
audit = (
    metrics.groupby("source")
    .agg(metrics=("eligible", "sum"), expected=("series_id", "count"), oldest_date=("date", "min"), latest_date=("date", "max"))
    .reset_index()
    .sort_values("metrics", ascending=False)
)
audit["latest_date"] = audit["latest_date"].dt.strftime("%Y-%m-%d").fillna("N/D")
audit["oldest_date"] = audit["oldest_date"].dt.strftime("%Y-%m-%d").fillna("N/D")
audit.loc[audit["source"] == "CBO Open Data", ["oldest_date", "latest_date"]] = "Projection (févr. 2026)"
for _, row in audit.iterrows():
    st.markdown(
        '<div class="detail-item">'
        f'<strong>{html.escape(str(row["source"]))}</strong> · {row["metrics"]}/{row["expected"]} signaux utilisables'
        '<div class="detail-meta">'
        f'Observations : {html.escape(str(row["oldest_date"]))} à {html.escape(str(row["latest_date"]))}'
        '</div></div>',
        unsafe_allow_html=True,
    )

render_site_footer()
