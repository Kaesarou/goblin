# Exploitation Alpaca démo

Cette procédure concerne `BROKER=alpaca_demo` (alias `alpacademo`) sur la branche
expérimentale. Les ordres vont au compte papier Alpaca. Les modes `alpaca_live`
et `etoro_live` restent refusés au démarrage. Aucun compte réel n'a servi à la
validation automatisée ; les transports Alpaca y sont simulés.

## Deux conteneurs sur le même VPS

Le conteneur eToro et le conteneur Alpaca doivent avoir des noms, fichiers `.env`
et montages de données distincts. Ils peuvent tous deux utiliser `/app/data`
dans leur conteneur si ce chemin correspond à deux répertoires différents du VPS.
Conserver tous les chemins SQLite, journaux, cache et garde de redémarrage Alpaca
dans son propre répertoire. Ne pas recopier la base eToro pour initialiser Alpaca.
Le premier démarrage Alpaca exige un répertoire dédié vide.

Le runtime lie les données Alpaca au mode et à l'identifiant de compte obtenu
auprès du broker. Il conserve aussi un verrou pendant les appels en cours et
la sauvegarde finale. Une ancienne version eToro ne respecte pas ce nouveau
verrou : la séparation physique des montages reste indispensable.

Avant le premier démarrage, vérifier :

- les clés du compte papier, `BASE_CURRENCY=USD` et le choix explicite IEX ou SIP ;
- une watchlist composée uniquement d'actions/ETF américains pris en charge,
  avec les permissions de négociation fractionnée ;
- un benchmark américain explicitement choisi et disponible chez Alpaca.
  `SPX500` n'est pas transformé en `SPY` automatiquement. Les benchmarks restent
  des données de contexte, sans ordre propre ;
- les sessions et le fuseau choisis dans le `.env` ;
- le montage persistant contenant la base V3 **et** le journal d'ordres dérivé :
  `POSITION_STORE_PATH.alpaca_demo.orders.sqlite`.

Le `.env` transmis pour relecture doit masquer les clés et secrets. Le fichier
Compose du dépôt n'a pas besoin d'être modifié pour ce second conteneur.

## Arrêt et reprise

Pour le conteneur nommé `goblin-alpaca`, un arrêt avec un délai adapté peut être
demandé ainsi (adapter le nom à celui du déploiement) :

```bash
docker stop --timeout 120 goblin-alpaca
docker inspect --format '{{.State.Running}}' goblin-alpaca
```

Attendre `false` avant de sauvegarder ou déplacer les fichiers. `SIGTERM` demande
un arrêt propre : arrêt des flux, fin des appels déjà lancés, projection des
résultats et sauvegarde. Un second signal ne coupe pas cette phase. Un appel
ralenti par le réseau ou une limite API peut dépasser le délai Docker ; le
`SIGKILL` qui suit ne produit alors pas de checkpoint de fin. Le journal durable
sert à récupérer l'issue des ordres au redémarrage.

Sauvegarder l'ensemble du répertoire dédié après arrêt, y compris les deux bases,
leurs éventuels fichiers SQLite auxiliaires, `runtime_storage.json`, les journaux
et les checkpoints. Le cache d'instruments est distinct du journal d'exécution :
ne pas traiter le journal d'ordres comme un cache jetable.

Redémarrer une seule instance avec le même compte, le même mode et les mêmes
montages. Vérifier le manifeste du nouveau run (`broker.account_id`, `data_feed`,
`universe_preflight`), les événements de récupération et les bloqueurs d'entrée.
Une coupure après exécution mais avant réception de la réponse ne doit pas créer
un second POST ; les recherches utilisent l'identifiant client déjà persisté.

Un arrêt normal produit un manifeste `completed` ou `interrupted`. Après une
interruption forcée, le manifeste précédent peut rester `running` : consulter
le processus réel et le nouveau run plutôt que d'interpréter ce champ isolément.
Les tests OS couvrent ces reprises ; le lancement de l'image sur le VPS reste
le test de l'environnement Docker, des droits du compte et du réseau réel.

## Ordre ou compte bloqué

| Observation | Traitement prévu |
| --- | --- |
| Ordre connu, réponse perdue ou état encore non terminal | Recherche REST avec le même identifiant client ; réservation conservée jusqu'à une preuve terminale. Aucun renvoi du POST. |
| Exécution terminale complète ou partielle confirmée | Application unique des quantités et du prix confirmés. Les autres causes de blocage restent actives. |
| Recherche d'ordre en 404 | Résultat insuffisant pour conclure à un rejet. La recherche reste possible ; le temps écoulé ne libère pas la réservation. |
| Événement de début V3 sans réservation correspondante dans le journal Alpaca | Blocage conservé. Aucun ordre de remplacement et aucune déclaration automatique d'échec. |
| Position/ordre extérieur, différence de quantité ou correction d'une exécution déjà terminale | Revue de l'activité du compte requise. Les positions agrégées ne sont pas réaffectées arbitrairement aux lots Goblin. |
| Identité de compte, mode ou répertoire incompatible | Démarrage refusé ; retrouver le montage et les clés du compte d'origine. |

Pour un incident durable :

1. Relever le run, le symbole, l'action V3, le `client_order_id` et, s'il est connu,
   l'identifiant d'ordre Alpaca. Garder les horaires UTC et les erreurs REST.
2. Arrêter le conteneur concerné et sauvegarder son répertoire complet. Ne pas
   arrêter ou modifier le conteneur eToro pour résoudre un incident Alpaca.
3. Comparer, en lecture seule, le journal V3, le journal Alpaca et l'historique
   du compte papier correspondant. Conserver les preuves de statut, quantité
   exécutée et prix. Un portefeuille vide ou une réponse 404 isolée ne prouve
   pas qu'aucun ordre n'a été envoyé.
4. Si la preuve attendue est disponible, laisser le chemin de récupération normal
   la lire au redémarrage. Si elle manque définitivement ou contredit les données
   persistées, garder cette instance arrêtée et préparer une réconciliation
   spécifique revue sur une copie des données, avec une trace d'audit.

Il n'existe pas encore de commande Alpaca qui force une réconciliation. Le script
`scripts/acknowledge_v3_broker_reconciliation.py` est réservé à eToro. Ne pas
effacer une réservation, un défaut de preuve ou l'identité SQLite, ni injecter
un faux événement d'exécution pour rendre le runtime redémarrable. Les splits,
changements de symbole et autres opérations sur titres demandent également une
réconciliation explicite ; leur traitement automatique sort du périmètre démo.

Les estimations de frais eToro5 restent celles de la stratégie de recherche
figée. Elles ne représentent pas les frais Alpaca réels ni une preuve de
performance équivalente entre brokers.
