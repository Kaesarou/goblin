# Replay breakeven MANAGED_EDGE_V1

Replay stateful des 22, 23, 24, 27, 28 et 31 juillet 2026. Les montants sont dans la devise du compte des journaux et les prix de sortie sont exécutables (BUY au bid, SELL au ask).

## Résultats agrégés

| Variante | Brut réalisé | Coûts explicites | Net réalisé | MTM net | Net + MTM | Trades | DD réalisé | DD equity intraday |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Baseline corrigée 0,55/0,60 | 125.0830 | 96.3963 | 28.6867 | -3.7281 | 24.9586 | 44 | 23.1392 | 26.8373 |
| Variante 0,65/0,70 | 130.6052 | 94.2004 | 36.4048 | -3.0488 | 33.3560 | 43 | 23.1392 | 26.8373 |

Delta réalisé variante : **+7.7181**. Sur les 5 journées complètes, le delta n’est que **+1.2935** (-4.8250 contre -3.5315).

## Par journée

| Date | Complétude | Candidates | Baseline réalisée | Variante réalisée | Delta | Baseline MTM | Variante MTM |
|---|---|---:|---:|---:|---:|---:|---:|
| 2026-07-22 | complète | 584 | 8.4139 | 8.2499 | -0.1640 | 0.0000 | 0.0000 |
| 2026-07-23 | complète | 719 | -22.7798 | -22.7798 | 0.0000 | 0.0000 | 0.0000 |
| 2026-07-24 | complète | 674 | 13.2256 | 13.2256 | 0.0000 | 0.0000 | 0.0000 |
| 2026-07-27 | complète | 700 | -2.8061 | -2.8061 | 0.0000 | 0.0000 | 0.0000 |
| 2026-07-28 | incomplète | 869 | 33.5117 | 39.9363 | 6.4246 | -3.7281 | -3.0488 |
| 2026-07-31 | complète | 829 | -0.8786 | 0.5789 | 1.4575 | 0.0000 | 0.0000 |

## Segments

| Segment | Baseline trades | Baseline net | Variante trades | Variante net |
|---|---:|---:|---:|---:|
| EU | 18 | 12.8323 | 18 | 12.6683 |
| US | 26 | 15.8544 | 25 | 23.7365 |
| BUY | 35 | 48.6168 | 35 | 57.1179 |
| SELL | 9 | -19.9301 | 8 | -20.7131 |

## Lifecycle et contraintes

| Mesure | Baseline | Variante |
|---|---:|---:|
| Sorties `initial_stop` | 4 | 4 |
| Sorties `protected_breakeven` | 19 | 15 |
| Sorties `protected_trailing` | 9 | 10 |
| Sorties `session_force_close` | 1 | 2 |
| Sorties `stale_exit` | 5 | 5 |
| Sorties `take_profit` | 6 | 7 |
| Durée moyenne (min) | 62.6490 | 68.3583 |
| MFE moyen (%) | 0.7981 | 0.8357 |
| MAE moyen (%) | 0.4077 | 0.4191 |
| Capital-minutes | 2024850.7161 | 2202926.8831 |
| Pic positions simultanées | 5.0000 | 5.0000 |
| Empêchés par capacité | 6.0000 | 6.0000 |
| Empêchés par cooldown | 28.0000 | 35.0000 |
| Pending entries ouvertes | 0.0000 | 0.0000 |
| TP après sortie breakeven | 5.0000 | 3.0000 |
| SL après sortie breakeven | 3.0000 | 3.0000 |
| Delta net contrefactuel si protections conservées | 24.8472 | 17.5215 |
| Plus gros gain | 9.2793 | 9.2793 |
| Part du plus gros gain dans les gains positifs | 11.1000% | 10.1300% |

## Sorties modifiées par le seuil

| Date | Position | Baseline | Variante | Délai (s) | Delta net |
|---|---|---|---|---:|---:|
| 2026-07-22 | ENI.MI BUY | protected_breakeven | protected_breakeven | 4107.734 | -0.1640 |
| 2026-07-28 | ITX.MC BUY | protected_breakeven | protected_breakeven | 2734.147 | +0.0000 |
| 2026-07-28 | GEV BUY | protected_breakeven | take_profit | 922.853 | +6.5965 |
| 2026-07-31 | MU SELL | protected_breakeven | session_force_close | 5036.327 | -0.6111 |
| 2026-07-31 | MU BUY | protected_breakeven | protected_trailing | 1127.992 | +2.0686 |

## Journée incomplète du 28 juillet

| Variante | Réalisé | MTM | Positions ouvertes | Détail MTM |
|---|---:|---:|---:|---|
| Baseline | 33.5117 | -3.7281 | 1 | AMD SELL: -3.7281 |
| Variante | 39.9363 | -3.0488 | 2 | INTC SELL: +0.6793, AMD SELL: -3.7281 |

## Méthodologie et limites

- Validation CRC des archives du plan : `passed`.
- Sélection : `managed_edge_v1` ; top-N puis contraintes portefeuille, sans repêchage.
- Les fills de clôture broker historiques ne sont pas disponibles : les entrées et sorties du replay sont des estimations exécutables bid/ask.
- Le spread est inclus dans les côtés exécutables et n’est pas retranché une seconde fois ; seuls les coûts explicites estimés sont déduits.
- Le DD réalisé suit les clôtures confirmées. Le DD equity intraday marque aussi chaque position ouverte au dernier côté exécutable et retient le pire drawdown d’une séance.
- Le 28 juillet s’arrête au dernier tick enregistré. Ses positions restantes sont conservées et valorisées, sans fin de séance inventée.
- Le JSON associé contient chaque trade, MFE/MAE, provenance, position ouverte, contribution temporelle et contrefactuel.
