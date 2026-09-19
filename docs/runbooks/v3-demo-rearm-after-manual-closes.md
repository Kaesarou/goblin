# Goblin V3 DEMO — reprise après clôtures manuelles (PR #85)

Cette livraison remplace **uniquement** le verrou permanent de la release d'observation du 19 septembre 2026. Elle ne modifie ni la stratégie ETORO5 ni la gestion des exits V3. Les dix positions du compte DEMO sont sous clôture **manuelle** et ne doivent pas être importées fictivement dans le registre V3 neuf. Une clôture demandée n'est pas une clôture exécutée.

## Avant la reprise

- Le conteneur garde `app.runtime.restart_guard` comme parent du processus applicatif. Le disjoncteur persistant lance d'abord `scripts.demo_rearm_after_manual_closes`, sans lancer V3 ni émettre d'ordre broker tant que le compte n'est pas à plat. Une fois les contrôles validés, le watcher **remplace son propre processus par `app.main` via `execv`**, préservant le PID et la transmission de SIGTERM pendant les déploiements et arrêts Docker.
- Toutes les trois minutes, le watcher réalise uniquement un GET portefeuille DEMO et un GET P&L DEMO avec les clients résilients. Il exige **deux contrôles consécutifs** indiquant zéro position ouverte et zéro ordre d'ouverture en attente (`ordersForOpen` et `orders`). Une réponse malformée, un échec réseau ou une nouvelle position remet le compteur à zéro. Les ordres de clôture en attente ne sont jamais assimilés à des fills.
- Avec un SQLite V3 neuf, si les deux contrôles réussissent, l'ancien marqueur `data/v3_external_broker_activity.json` est archivé dans le **même volume persistant** sous `v3_external_broker_activity.acknowledged.*.json`. Il n'est jamais supprimé, et ni le SQLite ni les journaux ne sont remis à zéro. Le runtime V3 refait sa propre réconciliation broker ; si des positions réapparaissent, les BUY restent bloqués.
- Si un registre V3 non vide existe déjà, le watcher laisse le démarrage normal et la réconciliation V3 gérer les positions **à condition qu'aucun marqueur externe ne soit actif**. Un registre non vide et un marqueur externe simultanés sont bloquants.
- Le mode est limité à `BROKER=etoro_demo` et à un compte USD. Aucun support du compte réel. `GOBLIN_OBSERVATION_ONLY=1` continue à bloquer V3 si explicitement réactivé.

## Ce que signifie « déploiement réussi »

Le workflow vérifie l'image immuable, un conteneur en vie pendant 120 secondes et le schéma des GET DEMO ; il n'affirme **pas** que les clôtures ont eu lieu ni que le moteur est déjà armé. Voir les messages `DEMO_REARM_WAIT` et `DEMO_REARM_READY` dans les journaux Docker. Le healthcheck ne mesure que la vie du processus.

## Audit du lundi soir

Conserver le ZIP de `data/logs` et vérifier : passage réel de `DEMO_REARM_WAIT` à `DEMO_REARM_READY`, préflight V3 réussi, absence de positions broker non suivies, equity DEMO, absence de 429/retries anormaux et restart storm, puis pour chaque premier BUY l'identifiant d'ordre/position, le fill, les unités et le notionnel en USD enregistré dans `ENTRY_FILLED`. Le `investedAmountCurrency=1` ne doit jamais se substituer au capital réellement exposé : comparer au `clientPortfolio.positions[].amount` pour le même `positionID`. Ne pas conclure à une exécution fiable à partir d'un test de week-end sans trade.
