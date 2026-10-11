"""
Debt Risk Radar.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import html
import os
from base64 import b64encode
from datetime import datetime
from pathlib import Path

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
                  "official_delayed": "Publication officielle différée",
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

assets = Path(__file__).resolve().parent / "assets"
font_data = b64encode((assets / "fonts/jetbrains-mono-latin.woff2").read_bytes()).decode("ascii")
styles = (assets / "theme.css").read_text(encoding="utf-8")
st.markdown("<style>" + styles.replace("__L0G_FONT__", font_data) + "</style>", unsafe_allow_html=True)


def metric_card(label: str, value: str, sub: str, color: str) -> str:
    if value == "N/D":
        color = "#8b909b"
    label = html.escape(label)
    value = html.escape(value)
    sub = html.escape(sub)
    color = html.escape(color)
    return f"""
    <div class="metric-card" style="--metric-color: {color};">
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
            f'<div class="risk-bar"><span style="width:{width:.0f}%;background:{score_color(risk_score)}"></span></div>'
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
        margin=dict(l=10, r=18, t=56, b=32),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e7e9ee", family="JetBrains Mono, ui-monospace, monospace", size=12),
        title_font=dict(size=15, color="#f5f6f8"),
        legend=dict(orientation="h", font=dict(size=11)),
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.16)", tickfont=dict(color="#8b909b")),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.16)", tickfont=dict(color="#8b909b")),
    )
    if yaxis_title:
        fig.update_yaxes(title_text=yaxis_title)
    return fig


def render_header(view: str) -> None:
    radar_active = "active" if view == "radar" else ""
    faq_active = "active" if view == "faq" else ""
    radar_current = 'aria-current="page"' if view == "radar" else ""
    faq_current = 'aria-current="page"' if view == "faq" else ""
    st.markdown(
        f"""
        <header class="site-header">
            <div class="network-bar">
                <a class="l0g-logo" href="https://l0g.fr/" target="_self" aria-label="l0g.fr, accueil">
                    <span>l</span><span class="logo-zero">0</span><span>g</span><span class="logo-cursor" aria-hidden="true">_</span>
                </a>
                <a class="home-link" href="https://l0g.fr/" target="_self">Retour à l0g.fr <span aria-hidden="true">↗</span></a>
            </div>
            <div class="dashboard-heading">
                <span class="scope-label">// DETTE US</span>
                <h1>Debt Risk Radar</h1>
                <p class="tagline">Dette souveraine · crédit privé · tensions de marché</p>
            </div>
            <nav class="top-nav" aria-label="Navigation">
                <a class="nav-link {radar_active}" href="?" target="_self" {radar_current}>Radar</a>
                <a class="nav-link {faq_active}" href="?view=faq" target="_self" {faq_current}>Aide / FAQ</a>
                <a class="nav-link methodology-link" href="https://l0g.fr/methodologie/debt-risk-radar/" target="_self">Méthodologie <span aria-hidden="true">↗</span></a>
            </nav>
        </header>
        """,
        unsafe_allow_html=True,
    )


def render_site_footer() -> None:
    st.markdown(
        """
        <footer class="site-footer" aria-label="Pied de page Debt Risk Radar">
          <div class="footer-brand">
            <a class="l0g-logo" href="https://l0g.fr/" target="_self" aria-label="l0g.fr, accueil">
              <span>l</span><span class="logo-zero">0</span><span>g</span><span class="logo-cursor" aria-hidden="true">_</span>
            </a>
            <span>Debt Risk Radar · MIT License</span>
          </div>
          <nav class="footer-nav" aria-label="L’écosystème l0g">
            <a href="https://l0g.fr/" target="_self">l0g.fr <span aria-hidden="true">↗</span></a>
            <a href="https://l0g.fr/dashboards/" target="_self">Tous les dashboards</a>
          </nav>
        </footer>
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

            Si un renouvellement échoue, la dernière réponse validée reste utilisable dans
            une limite maximale distincte de la cadence de renouvellement. Son ancienneté
            et sa réutilisation sont signalées. La pause et l'heure de reprise autorisée sont
            signalées séparément. À l'expiration, le signal devient indisponible et le score
            courant est suspendu si sa couverture est incomplète.
            """
        )

    with st.expander("À quelle fréquence les données se rafraîchissent-elles ?"):
        st.markdown(
            f"""
            L'app se rafraîchit automatiquement toutes les `{AUTO_REFRESH_SECONDS // 60}` minutes.
            Le renouvellement vise six heures pour Treasury et FRED, et vingt-quatre heures
            pour BIS, CBO et World Bank, avec une anticipation maximale de trente minutes.
            En cas d'échec, une réponse validée peut être réutilisée pendant au maximum
            48 heures depuis sa collecte pour Treasury/FRED, sept jours pour BIS/World Bank,
            et trente jours pour le millésime CBO figé. La mention **cache source validé**
            distingue cette situation d'une collecte renouvelée. Les limites d'âge des
            observations économiques restent applicables et peuvent expirer plus tôt.
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
        st.markdown(
            "Pour les séries FRED trimestrielles proches de cette limite, le collecteur vérifie "
            "aussi les métadonnées officielles, renouvelées toutes les six heures et réutilisables "
            "au maximum 48 heures depuis leur collecte en cas de panne. Une observation "
            "reste utilisable avec la mention **publication officielle différée** uniquement si elle "
            "correspond au dernier trimestre annoncé par FRED, si la mise à jour de la série date "
            "d'au plus 120 jours et si la fin du trimestre ne dépasse pas six mois plus 30 jours. "
            "Ces bornes sont une politique de surveillance, pas un engagement du fournisseur. "
            "Les réponses doivent toujours respecter leurs échéances de cache. La date de période "
            "et la valeur ne sont jamais modifiées ; une publication différée n'est pas une donnée du jour. "
            "Sans cette confirmation, ou au-delà des bornes, le signal est exclu."
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
delayed = metrics[metrics["quality"] == "official_delayed"]
cached = metrics[(metrics["cache_status"] == "cached") & metrics["eligible"]]
if not cached.empty:
    st.info("Cache source validé : certaines réponses dépassent leur cadence de renouvellement. "
            "Les valeurs et dates d'origine sont conservées dans les limites documentées ; "
            "une nouvelle publication peut ne pas encore être intégrée.")
    with st.expander("Sources réutilisées et échéances", expanded=False):
        for _, row in cached.iterrows():
            st.write(f"{row['name']} : réponse collectée {row['observation_checked_at']} ; "
                     f"échéance effective {row['freshness_expires_at']}.")
if not delayed.empty:
    st.info("Publication officielle différée : certains ratios trimestriels sont encore les dernières "
            "observations confirmées par FRED. Ils contribuent au score avec leur période d'origine, "
            "dans les limites documentées. Ils ne mesurent pas la situation du jour.")
    with st.expander("Publications différées", expanded=True):
        for _, row in delayed.iterrows():
            st.write(f"{row['name']} : période {row['date'].strftime('%Y-%m-%d')} ; "
                     f"mise à jour FRED {row['publication_updated_at']} ; "
                     f"métadonnées vérifiées {row['publication_checked_at']}.")
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
bucket_view["bucket_label"] += "<br>" + bucket_view["n"].astype(str) + "/" + bucket_view["expected"].astype(str) + " signaux"
bucket_view.loc[bucket_view["role"] == "structurel", "bucket_label"] += " · structurel"
fig_bucket = go.Figure(
    go.Bar(
        x=round(bucket_view["score"], 1),
        y=bucket_view["bucket_label"],
        orientation="h",
        marker=dict(color=[score_color(v) for v in bucket_view["score"]]),
        text=[f"{v:.1f}" if pd.notna(v) else "N/D" for v in bucket_view["score"]],
        textposition=["inside" if pd.notna(v) and v >= 75 else "outside" for v in bucket_view["score"]],
        insidetextanchor="end",
    )
)
fig_bucket.add_vline(x=WATCH_LEVEL, line_width=1, line_dash="dot", line_color="#f5b13d")
fig_bucket.add_vline(x=STRESS_LEVEL, line_width=1, line_dash="dot", line_color="#ff4d87")
chart_layout(fig_bucket, "", height=390)
fig_bucket.update_layout(
    height=390,
    xaxis=dict(range=[0, 100], title="Risk score"),
    yaxis=dict(autorange="reversed"),
    margin=dict(l=10, r=24, t=42, b=10),
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
