# V3 double ouverture INTC — incident de production DEMO (15–18 septembre 2026)

**État : investigation / PR DRAFT ; aucun correctif autorisé en production à ce stade.**

## Faits observés

- Image en production avant incident : `7ec9e8f5e800a7b57dd32dc46728450dd887c941` (`inventory_runtime_v3_8`).
- Le 15 septembre à 13:40:01.009430 UTC, `ORDER_SUBMISSION_STARTED` pour `INTC:55fcaea1e52b4ecb6f8935e4`, suivi de `ENTRY_FILLED` à 13:40:04.542386, position broker `3599774868`, 3.292115 unités à 99.27.
- À 13:40:03.380833 UTC, **avant le premier fill**, `ORDER_SUBMISSION_STARTED` pour `INTC:97c14413004a4203fb61ea12`, suivi de `ENTRY_FILLED` à 13:40:07.172395, position broker `3599774883`, 3.291612 unités à 99.27.
- Deux inventaires distincts pour le même symbole, aucun `EXIT_FILLED` connu pour ces deux inventaires dans le relevé du 18 septembre.
- Le 18 septembre, eToro DEMO affiche deux lignes INTC pour un total de 6.58373 unités, compatible avec la somme des deux fills du ledger (6.583727 unités). Les identifiants des lignes ne sont pas visibles dans les captures et doivent encore être vérifiés directement côté broker avant reprise.
- Runtime au 18 septembre : `ValueError: Multiple active inventories for INTC`, levée dans `InventoryBook.from_events()` avant démarrage des flux/du broker ; `goblin-bot` en boucle de redémarrage, puis arrêté par l'opérateur avec `docker update --restart=no goblin-bot`.

## Hypothèse technique à reproduire

`V3BrokerExecutor` n'utilise qu'une réservation par `intent_id` (`_pending_actions`). Deux intents BUY différents pour le même symbole peuvent donc déclencher deux soumissions tandis que `InventoryBook.active_for_symbol()` ne voit encore aucune position confirmée. La reconstruction refuse ensuite plusieurs inventaires actifs d'un même symbole.

**À prouver en test :** deux intents distincts reçus alors qu'une première ouverture est en attente ; les deux actions broker ne doivent jamais être soumises. Tester le chemin retour succès, rejet, exception, unknown et restart. Préserver la possibilité d'une entrée additionnelle *après résolution* si le planner l'autorise et si le même inventaire est réutilisé.

## Critères de sortie du correctif

1. Réservation atomique par symbole avant l'appel broker, libérée seulement quand l'issue est connue ; les ordres `UNKNOWN` restent bloqués jusqu'à résolution broker. Aucun déblocage sur simple timeout.
2. Reprise des deux fills **réellement exécutés**, sans supprimer ou réécrire leurs événements : deux legs broker, mêmes identifiants, quantités, prix et economics ; ne pas produire une fermeture artificielle.
3. Aucune modification arbitraire de positions eToro. Réconciliation préalable de toutes les positions ouvertes, notamment `3599774868` et `3599774883` ; vérifier les positions réellement ouvertes et leur quantité exacte.
4. Cohérence des projections à l'exécution et au restart, `active_for_symbol`, limite d'exposition, limite de 5 inventaires et close pro-rata ; ne pas cacher un second inventory dans un book qui ne gère qu'un inventory par symbole.
5. Test d'intégration sur copie du ledger (ordre réel des événements INTC), redémarrage à répétition sans doublon d'ordre, clôtures partielles séquentielles et `EXIT_ECONOMICS_CONFIRMED`, et présence d'au moins un autre symbole sans régression.
6. Pas de retuning stratégique, aucun effacement de logs, aucune migration destructive ni création/suppression automatique de positions.

## Sauvegarde et conservation des logs — BLOQUANT pour merge et déploiement

La collecte du 10 au 18 septembre contient des données prospectives nécessaires à l'audit de ce soir. Les journaux compressés et manifests sous `data/logs/runs/`, les autres journaux dans `data/logs/`, les éventuels fichiers de recherche, ainsi que `data/goblin.sqlite` et ses fichiers SQLite WAL/SHM ne doivent **pas** être purgés, tronqués, déplacés ni réinitialisés. `docker-compose.production.yml` monte `./data:/app/data` : une nouvelle image ne doit pas modifier ce volume au déploiement. Ne pas utiliser `docker compose down -v`, `docker volume prune`, `rm -rf data`, de procédure de rétention, ni de migration/snapshot qui écrase les originaux.

Avant toute reprise, effectuer une copie/snapshot cohérente de `data/` et de SQLite hors du chemin modifié par le runtime, vérifier les tailles et empreintes, contrôler la continuité des runs et archiver les logs de la semaine séparément. Une copie SQLite cohérente doit inclure les transactions WAL (via API backup ou arrêt confirmé + copie des fichiers appropriés). Documenter les identifiants des runs et la fenêtre temporelle non couverte depuis le premier crash. L'analyse de ce soir repose sur **les données brutes inchangées** et ne doit pas être retardée/contaminée par la récupération.

**Interdit sans validation explicite :** merge vers `develop`/`main`, déploiement production, redémarrage de Goblin ou exécution de scripts d'acquittement/altération de SQLite. Le processus actuellement arrêté laisse les positions broker sans surveillance automatique ; l'opérateur doit les surveiller directement dans eToro DEMO jusqu'à la reprise sûre.
