# Replay MANAGED V1 vs V2

**Statut : REJET EMPIRIQUE — V2 reste strictement en shadow.**

Le comparatif primaire oppose V2 à V1 avec le même filtre `quote_quality_v2`. L’univers de candidats est celui des journaux ; le replay ne prétend pas régénérer les bougies ni les candidats.

| Politique | Trades | Brut | Coûts | Net | Drawdown |
|---|---:|---:|---:|---:|---:|
| managed_v1_live_validation | 92 | 134.0249 | 201.7651 | -67.7402 | 97.4218 |
| managed_v1 | 92 | 109.5046 | 201.7651 | -92.2605 | 121.9421 |
| managed_v1_quote_quality_v2 | 92 | 115.5744 | 201.7651 | -86.1907 | 115.8723 |
| managed_v2 | 463 | -109.3431 | 1014.9483 | -1124.2914 | 1131.8250 |

## Validation des prix broker

165 ouvertures historiques ; 29 entrées broker explicites (17.6 %) et 26 sorties broker explicites (15.8 %). Les 136 prix legacy ambigus restent des fallbacks contractuels nommés.

## Effet isolé de la qualité des cotations

254 observations de cotation ont été mises en quarantaine. Sur cet univers journalisé, aucun identifiant de trade ne change et le net V1 varie de +6.0698.

Issues V1 effectivement modifiées :

- 2026-08-04 `GOOGL` (`4e8183d3b4a43c387828164e8c4c1ec38603a523cc7dfb1647e0f199b28b9052`) : `initial_stop` -9.3852 vers `stale_exit` -3.3154, delta +6.0698.

## Par segment

| Segment | V1 trades | V1 net | V2 trades | V2 net |
|---|---:|---:|---:|---:|
| EQUITY_EU_BUY | 36 | -58.1857 | 149 | -370.6959 |
| EQUITY_EU_SELL | 3 | -11.4698 | 70 | -125.8117 |
| EQUITY_US_BUY | 40 | 11.3814 | 119 | -299.8798 |
| EQUITY_US_SELL | 13 | -27.9166 | 125 | -327.9040 |

## Par journée

| Date | V1 validation live | V1 | V1 + qualité | V2 | Delta primaire |
|---|---:|---:|---:|---:|---:|
| 2026-07-22 | 8.4139 | 8.4139 | 8.4139 | -40.7693 | -49.1832 |
| 2026-07-23 | -22.7798 | -22.7798 | -22.7798 | -128.2818 | -105.5020 |
| 2026-07-24 | 13.2256 | 13.2256 | 13.2256 | -45.7548 | -58.9804 |
| 2026-07-27 | -2.8061 | -2.8061 | -2.8061 | -40.3154 | -37.5093 |
| 2026-07-28* | 33.5117 | 33.5117 | 33.5117 | -63.6310 | -97.1427 |
| 2026-07-29 | -23.1889 | -23.1889 | -23.1889 | -132.9427 | -109.7538 |
| 2026-07-30 | -51.2056 | -51.2056 | -51.2056 | -78.9359 | -27.7303 |
| 2026-07-31 | -0.8786 | -0.8786 | -0.8786 | -151.5545 | -150.6759 |
| 2026-08-03 | -7.8333 | -21.5959 | -21.5959 | -67.1759 | -45.5800 |
| 2026-08-04 | 13.7904 | 4.2025 | 10.2723 | -98.6169 | -108.8892 |
| 2026-08-05 | -0.9068 | -1.1059 | -1.1059 | -77.1422 | -76.0363 |
| 2026-08-06 | -2.9111 | -3.2105 | -3.2105 | -104.6663 | -101.4558 |
| 2026-08-07 | -24.1716 | -24.8429 | -24.8429 | -94.5047 | -69.6618 |

\* Journée incomplète : le tableau présente le net réalisé. Le mark-to-market et les positions encore ouvertes restent explicites dans le rapport JSON.

## Sélections

- Référence primaire : managed_v1_quote_quality_v2
- Overlap : 20
- Ajoutés par V2 : 443
- Retirés par V2 : 72
- Delta net : -1038.1007
