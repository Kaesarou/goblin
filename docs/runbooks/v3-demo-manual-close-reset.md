# Goblin V3 DEMO — remise à zéro après clôtures manuelles en attente

La fermeture manuelle d'une position à marché fermé peut laisser la **position encore ouverte** chez eToro tant que son ordre de clôture n'est pas exécuté. Ce n'est pas un ordre d'ouverture : les tableaux P&L `ordersForOpen` et `orders` ne prouvent **pas** l'absence d'ordres de clôture. Ne pas interpréter une demande de clôture acceptée comme un fill.

## Préparation sans mutation broker automatique

1. **Laisser le conteneur arrêté.** Vérifier dans eToro DEMO la liste de toutes les positions et soumettre, si souhaité, les clôtures **manuellement**. Ne pas envoyer une seconde clôture automatiquement sur les mêmes positions.
2. Archiver l'intégralité de `data/logs` et une copie cohérente du SQLite d'origine (`data/goblin.sqlite`, `-wal`, `-shm`) **hors du volume actif**, conteneur arrêté. Vérifier la copie et les empreintes avant toute remise à zéro. Ne jamais supprimer `data/`, les logs, les manifests ni `runtime_restart_guard.json` pour contourner un disjoncteur.
3. Après confirmation de la sauvegarde et uniquement si l'ancien ledger ne doit plus être restauré, déplacer les trois fichiers SQLite d'origine vers l'archive et laisser le runtime initialiser un SQLite neuf. Le choix de repartir avec un book vide est une décision opérateur, **pas** une preuve que le broker est vide.
4. Un démarrage contrôlé sur la nouvelle image interroge le portefeuille DEMO et les ordres d'**ouverture** en attente. Si le broker expose encore des positions non connues du SQLite neuf, ou des ouvertures en attente, Goblin démarre **en observation uniquement** : bougies, logs, métriques et lecture d'equity restent possibles ; `new_risk_allowed=False` ; aucun BUY ; aucune clôture émise pour ces positions externes.
5. **Pas de reprise automatique des BUY** à l'exécution des clôtures manuelles. Quand eToro confirme zéro position ET zéro ouverture en attente, arrêter puis redémarrer de manière contrôlée. Le preflight repart d'une nouvelle lecture broker. Ne pas repartir en trading sur une simple capture de demande de clôture ou un tableau `ordersForOpen` vide.

En cas de GET portefeuille/P&L indisponible ou malformé, le démarrage est refusé de façon conservatrice ; ce n'est pas une preuve de compte vide. Avec un **ancien** SQLite contenant des positions/événements, toute divergence de broker reste bloquante : le mode observation n'est permis que pour un ledger réellement neuf. Avec le vieux SQLite INTC incohérent, la reconstruction peut échouer avant ce mode ; archiver puis remettre à zéro comme prévu.

## Vérification du notionnel, lecture seule

Une fois les identifiants d'ordres historiques connus, `python scripts/inspect_etoro_notional_readonly.py --order-id <ID>` fait deux GET DEMO (lookup ordre et P&L) et imprime uniquement les champs économiques de l'exécution, sans identifiants de compte, headers ou clés. Après clôture d'une position, elle peut ne plus apparaître dans le P&L courant. Une absence de résultat ne justifie ni un montant inventé ni une reprise de BUY : le garde-fou d'exposition reste actif jusqu'à vérification de la source broker.

## Exploitation / redémarrages

`docker-compose.production.yml` utilise `on-failure:5` avec un disjoncteur persistant de **cinq démarrages en 30 minutes**. Une fois la limite atteinte, le conteneur reste arrêté et nécessite une revue humaine. Contrairement à `unless-stopped`, `on-failure` ne garantit pas une relance automatique au redémarrage du démon Docker : prévoir une supervision et une procédure de redémarrage contrôlé, sans relance aveugle d'un compte non réconcilié. Le healthcheck du Compose vérifie seulement l'existence du processus, pas l'autorisation d'ouvrir des positions.

**Aucune étape de cette procédure ne nécessite de merger, déployer ou agir sur le VPS avant une autorisation distincte.**
