# Sources publiques pour comprendre et reproduire PredictionArena (Polymarket)

Dernière vérification des liens : **12 juillet 2026**.

Objectif de ce document : rassembler les sources publiques permettant de reconstruire le fonctionnement du benchmark PredictionArena et, séparément, les briques Polymarket nécessaires à une reproduction opérationnelle. Les niveaux de priorité sont indicatifs :

- **P0 — direct / primaire** : source publiée par PredictionArena, ses créateurs ou le papier du projet.
- **P1 — infrastructure** : documentation officielle ou code maintenu par Polymarket.
- **P2 — reproduction / recherche** : code, données ou travaux scientifiques utiles pour tester et valider une implémentation.
- **P3 — contexte / corroboration** : témoignages, articles secondaires et annonces sociales ; utiles mais à vérifier contre les sources P0/P1.

## 1. Parcours de lecture recommandé

1. Lire le [papier en HTML](https://arxiv.org/html/2604.07355v1), surtout les sections **4.4 Polymarket Implementation**, **5.2 Trading Protocol**, **5.3 Valuation**, **5.4 Risk Management** et l’annexe.
2. Comparer avec la [méthodologie publique](https://www.predictionarena.ai/methodology?platform=polymarket) et le [changelog](https://www.predictionarena.ai/changelog?platform=polymarket).
3. Implémenter le flux de données avec [l’overview des API Polymarket](https://docs.polymarket.com/api-reference/introduction), puis vérifier les ordres avec [Create Order](https://docs.polymarket.com/trading/orders/create) et les flux temps réel avec [WebSocket](https://docs.polymarket.com/market-data/websocket/overview).
4. Utiliser [PolyBench](https://github.com/PolyBench/PolyBench) et les jeux de données listés plus bas pour construire un replay/backtest avant tout essai live.

## 2. Sources directes PredictionArena — priorité P0

### 2.1 Site, état du benchmark et traces publiques

- [PredictionArena — onglet Polymarket](https://www.predictionarena.ai/?platform=polymarket) — page d’accueil, sélecteur de plateforme, historique, activité live, leaderboard, modèles et marchés. Au moment de la vérification, l’onglet Polymarket affichait des données live chargées dynamiquement, avec une mise à jour lente pouvant prendre jusqu’à 10 secondes ; conserver cette URL comme référence historique.
- [Méthodologie PredictionArena — route Polymarket](https://www.predictionarena.ai/methodology?platform=polymarket) — cycle de décision, capital initial, métriques, prompt système/utilisateur, outils, mark-to-market, gestion des positions, filtres et limites. Une partie du contenu peut être générique ou retomber sur la variante Kalshi dans le HTML rendu côté serveur : la section 4.4 du papier est la référence explicite pour Polymarket.
- [Changelog PredictionArena — route Polymarket](https://www.predictionarena.ai/changelog?platform=polymarket) — évolution chronologique du prompt, du temps de traitement, des modèles, des métriques et du mode paper trading.
- [Page modèle Claude Opus 4.6 — Polymarket](https://www.predictionarena.ai/models/claude-opus-4-6?platform=polymarket) — exemple de page historique de modèle : positions, transactions, règlements et raisonnement ils sont disponibles mais sont chargées dynamiquement
- [Page modèle Grok 4.20 Checkpoint — Polymarket](https://www.predictionarena.ai/models/grok-4-20-checkpoint?platform=polymarket) — autre page historique utile pour comparer les traces par modèle.
- [Conditions d’utilisation](https://www.predictionarena.ai/terms-and-conditions) — confirme l’entité Arcada Labs Incorporated, la propriété du code/contenu et le cadre juridique ; ne décrit pas l’algorithme.
- [Politique de confidentialité](https://www.predictionarena.ai/privacy-policy) — informations sur les journaux et données de fonctionnement du service ; intérêt secondaire pour comprendre les traces potentiellement collectées.

### 2.2 Papier fondateur et versions figées

- [Prediction Arena — article arXiv, page HTML](https://arxiv.org/html/2604.07355v1) — **source la plus complète**. Décrit l’architecture frontend/orchestration/exécution/données, le cycle, le discovery system Polymarket, les outils, les plans/croyances, les limites de risque, la valorisation au bid, le netting, les cohortes live/paper et les limites expérimentales.
- [Prediction Arena — résumé arXiv](https://arxiv.org/abs/2604.07355) — métadonnées, résumé, auteurs, période d’évaluation et liens vers PDF/HTML/TeX.
- [Prediction Arena — PDF](https://arxiv.org/pdf/2604.07355) — version figée à conserver pour les formules, tableaux et figures ; utile lorsque le rendu HTML masque certaines équations.
- [Source TeX arXiv](https://arxiv.org/e-print/2604.07355v1) — archive de la source de la v1 ; utile pour rechercher précisément les noms de sections, tableaux, figures et formulations originales.

### 2.3 Créateurs, annonces et contexte de conception

- [Annonce Arcada Labs / The Intelligence Company sur LinkedIn](https://www.linkedin.com/posts/arcadalabs_introducing-prediction-arena-weve-allocated-activity-7417249200247275520-8tGr) — annonce initiale : six modèles, 60 000 dollars agrégés, marchés Kalshi, contexte complet, web search et notes persistantes ; mentionne que le harness vient de l’expérience Design Arena.
- [Publication de Grace Li sur le lancement](https://www.linkedin.com/posts/grace-li-721a4017b_we-just-dropped-60k-on-six-ai-models-to-activity-7418375766083596289-dQZ2) — même annonce avec les échanges publics et les premières questions sur la taille des paris, les outils et la méthode.
- [Podcast Delta — Grace Li, cofondatrice d’Arcada Labs](https://podcasts.apple.com/se/podcast/ep-56-grace-li-design-arena-co-creator-and-arcada/id1854467446?i=1000770011686&l=en-GB) — épisode consacré à Arcada, aux benchmarks réalistes, à Prediction Arena et à l’agent runner/harness ; les repères temporels indiquent les passages techniques.
- [Arcada Labs](https://arcada.dev/) — site de l’organisation et présentation de Prediction Arena comme expérience d’évaluation d’agents dans le monde réel.
- [Compte Prediction Arena sur X](https://x.com/predictionbench) — annonces, résultats et changements de modèles ; source sociale à archiver car les posts ou pages peuvent disparaître.
- [Post de Grace Li sur la nature du benchmark](https://x.com/grx_xce/status/2028584268721713551) — présente l’expérience comme une évaluation longue, temps réel, combinant découverte d’informations, décision sous incertitude et rendement lié au caractère contrariant.

## 3. Ce que les sources P0 permettent de reconstruire

Les éléments ci-dessous sont explicitement documentés, mais il faut distinguer les règles communes et les détails propres à Kalshi. Le papier est la source de référence pour l’adaptation Polymarket.

- **Cycle** : décision autonome environ toutes les 30 minutes ; synchronisation des marchés, règlement, construction du contexte, raisonnement, exécution, recalcul des métriques.
- **Contexte dynamique** : date/heure, données de marché, portefeuille, historique récent des règlements et trades, section d’apprentissage critique, raisonnement précédent et protocole de décision.
- **Protocole de décision** : choix de stratégie, recherche, estimation de probabilité ou de cible de prix, vérification du côté gagnant, valeur attendue après frais, taille, revue du portefeuille, exécution.
- **Outils** : recherche Web, mémoire/notes persistantes ; sur Polymarket, découverte de marchés, filtres de volume/volatilité/échéance, gestion de compte et outils de croyances/plans.
- **Exécution Polymarket** : ordres immédiats, achats de fractions de parts ; en live, une absence de liquidité ou de contrepartie peut rejeter l’ordre. Le papier trading supprime cette contrainte, donc ses résultats ne sont pas directement comparables.
- **Risque** : limite de concentration de 15 % par marché, contrôle de solvabilité avec frais et capital isolé/virtuel de 10 000 dollars par agent. Le plafond de dépense par cycle est mentionné sur la méthodologie, mais sa valeur exacte n’est pas publiée.
- **Valorisation** : cash + positions valorisées au bid disponible, pas au prix d’entrée ; moyenne pondérée du prix d’entrée pour les achats successifs.
- **Données et journalisation** : le papier mentionne PostgreSQL/Supabase, les snapshots de performance, les trades, les règlements, les traces de raisonnement, les appels d’outils et les métriques de tokens/temps.

### Ce qui reste inconnu publiquement

- dépôt officiel du code du harness PredictionArena ;
- texte intégral et schémas JSON des prompts système et utilisateur ;
- noms exacts, paramètres et seuils des outils de discovery Polymarket ;
- valeur exacte du plafond de dépense par cycle et tous les détails des garde-fous ;
- modèle de coût/latence utilisé pour chaque ordre et format exact des erreurs renvoyées au modèle ;
- paramètres de sampling des modèles, modèle de mémoire interne et algorithme de génération de la section « critical learning » ;
- schéma de base de données, endpoints internes et historiques complets des décisions Polymarket.

En pratique, l’objectif réaliste est une **réimplémentation comportementale** fondée sur les spécifications publiques, puis une validation par replay et tests d’ablation — pas une preuve de clonage bit à bit du service propriétaire.

## 4. Documentation officielle Polymarket — priorité P1

### 4.1 Vue d’ensemble et découverte des marchés

- [Index machine-readable de la documentation](https://docs.polymarket.com/llms.txt) — index compact des pages officielles, pratique pour automatiser la veille documentaire.
- [Introduction aux API Polymarket](https://docs.polymarket.com/api-reference/introduction) — séparation Gamma API (marchés/événements), Data API (positions/trades) et CLOB API (prix/carnet/ordres).
- [Market Data Overview](https://docs.polymarket.com/market-data/overview) — modèle Event/Market, token IDs, condition IDs, prix implicites et endpoints publics sans authentification.
- [Fetching Markets](https://docs.polymarket.com/market-data/fetching-markets) — récupération par slug, tag, événement, statut, volume, liquidité et pagination ; base de l’outil de discovery.
- [Recherche marchés/événements/profils](https://docs.polymarket.com/api-reference/search/search-markets-events-and-profiles) — endpoint `public-search` et paramètres de recherche, tags et statuts.
- [Lister les marchés](https://docs.polymarket.com/api-reference/markets/list-markets) — filtres d’activité, liquidité, volume, dates, tags, types sportifs et critères de résolution.
- [Obtenir un marché par ID](https://docs.polymarket.com/api-reference/markets/get-market-by-id) — schéma détaillé d’un marché : règles, dates, tokens, liquidité, best bid/ask, tick size, minimum order size, frais et statut.
- [Obtenir les trades d’un utilisateur ou d’un marché](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets) — historique des exécutions, côté, taille, prix, token, condition ID, timestamp et transaction on-chain.

### 4.2 Prix, liquidité et flux temps réel

- [Prix et carnet d’ordres](https://docs.polymarket.com/concepts/prices-orderbook) — différence entre midpoint, bid, ask, spread et prix réellement payé ; indispensable pour reproduire le mark-to-market et l’exécution.
- [Obtenir un carnet d’ordres](https://docs.polymarket.com/api-reference/market-data/get-order-book) — snapshot L2 avec bids, asks, timestamp, tick size, minimum order size, `neg_risk` et dernier prix.
- [Obtenir plusieurs carnets d’ordres](https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body) — endpoint batch pour construire un contexte multi-marchés efficacement.
- [Obtenir les prix de plusieurs tokens](https://docs.polymarket.com/api-reference/market-data/get-market-prices-query-parameters) — prix d’achat/vente par token, utile pour calculer edge, spread et coût d’exécution.
- [WebSocket — vue d’ensemble](https://docs.polymarket.com/market-data/websocket/overview) — canaux market, user, sports et RTDS ; types d’événements et authentification.
- [WebSocket — market channel](https://docs.polymarket.com/market-data/websocket/market-channel) — snapshots de carnet, changements de prix, derniers trades, best bid/ask, nouveaux marchés et résolutions.
- [Limites de débit](https://docs.polymarket.com/api-reference/rate-limits) — limites par endpoint ; à intégrer dans le synchroniseur et les outils de discovery.

### 4.3 Ordres, frais et règlement

- [Create Order](https://docs.polymarket.com/trading/orders/create) — types GTC/GTD/FOK/FAK, distinction entre remplissage immédiat et ordre restant, achat en montant et vente en nombre de parts.
- [Frais](https://docs.polymarket.com/trading/fees) — frais taker/maker, formule par marché, catégories avec ou sans frais et récupération des paramètres du marché ; indispensable pour le calcul d’EV.
- [Authentification CLOB](https://docs.polymarket.com/api-reference/authentication) — authentification L1 par clé/EIP-712 puis L2 par API key, secret, passphrase et HMAC ; signale les contraintes du funder wallet.
- [Clients L2](https://docs.polymarket.com/trading/clients/l2) — appels authentifiés pour ordres, annulations, trades et soldes avec les SDK officiels.
- [Résolution des marchés](https://docs.polymarket.com/concepts/resolution) — règles de résolution, UMA Optimistic Oracle, fenêtres de contestation, états 50/50 et rachat des positions gagnantes.
- [Conditional Token Framework](https://docs.polymarket.com/trading/ctf/overview) — tokens ERC-1155 Yes/No, collateralisation, split/merge/redeem et marchés `negRisk`.
- [Contrats Polymarket](https://docs.polymarket.com/resources/contracts) — adresses Polygon, CTF, exchanges, adaptateurs UMA et ressources d’audit.
- [Polymarket 101](https://docs.polymarket.com/polymarket-101) — vue pédagogique du modèle peer-to-peer, de la conservation des fonds et du règlement on-chain.

## 5. Code officiel ou maintenu par Polymarket — priorité P1/P2

- [Polymarket Agent Skills](https://github.com/Polymarket/agent-skills) — ensemble de références directement orientées agents : authentication, market data, order patterns, WebSocket, CTF, bridge et gasless. C’est la meilleure base pratique pour construire des outils comparables à ceux de PredictionArena.
- [Référence market-data des Agent Skills](https://github.com/Polymarket/agent-skills/blob/main/market-data.md) — exemples Gamma/Data/CLOB/Subgraph, pagination, mapping des tokens et estimation des fills.
- [Référence order-patterns](https://github.com/Polymarket/agent-skills/blob/main/order-patterns.md) — FAK/FOK/GTC/GTD, annulation, heartbeat, erreurs et patterns d’exécution.
- [Référence WebSocket](https://github.com/Polymarket/agent-skills/blob/main/websocket.md) — abonnements market/user et gestion des messages temps réel.
- [Référence CTF operations](https://github.com/Polymarket/agent-skills/blob/main/ctf-operations.md) — split, merge, redeem, tokens et marchés à risque négatif.
- [Polymarket Agents](https://github.com/Polymarket/agents) — ancien framework open source d’agents, aujourd’hui archivé ; intéressant pour voir une séparation Gamma/Polymarket/RAG/LLM et une intégration de trading complète, mais il ne s’agit pas du code PredictionArena.
- [Client Python CLOB v2](https://github.com/Polymarket/py-clob-client-v2) — SDK Python actuel pour lecture, authentification et ordres CLOB.
- [Client TypeScript CLOB v2](https://github.com/Polymarket/clob-client-v2) — SDK TypeScript actuel et exemples de construction/signature d’ordres.
- [Client Python CLOB historique](https://github.com/Polymarket/py-clob-client) — ancienne implémentation Python, archivée ; utile pour comparer les versions d’API et les migrations.
- [Polymarket CLI](https://github.com/Polymarket/polymarket-cli) — commandes pour marchés, carnets, trades, positions, open interest et leaderboard ; pratique pour vérifier une implémentation sans écrire immédiatement un client complet.
- [CTF Exchange v2](https://github.com/Polymarket/ctf-exchange-v2) — contrats Solidity de l’exchange CTF ; à consulter pour comprendre le règlement et la structure on-chain, pas seulement l’API.
- [Resolution Subgraph](https://github.com/Polymarket/resolution-subgraph) — indexation des résolutions et données liées au règlement.
- [Polymarket Subgraph](https://github.com/Polymarket/polymarket-subgraph) — index public de trades, volume, utilisateurs, liquidité et données de marché on-chain.

## 6. Travaux scientifiques, code et données pour la reproduction — priorité P2

### 6.1 Replays et benchmarks spécifiquement Polymarket

- [PolyBench — article](https://arxiv.org/abs/2604.14199) — benchmark construit à partir de snapshots temporels Polymarket, carnet CLOB et flux de nouvelles ; décrit une pipeline collecte → contexte multimodal → appel LLM → résolution.
- [PolyBench — code et données](https://github.com/PolyBench/PolyBench) — implémentation open source la plus directement exploitable pour un replay timestampé sans fuite d’information future ; inclut un format de décision structurée et une simulation d’exécution.
- [The Anatomy of a Decentralized Prediction Market](https://arxiv.org/abs/2604.24366) — microstructure Polymarket : profondeur, spreads, latence WebSocket, flux on-chain et différence entre trades du carnet et `OrderFilled`.
- [Code de réplication microstructure](https://github.com/philippdubach/polymarket-microstructure) — collecte WebSocket et jointure avec les événements on-chain ; utile pour vérifier la liquidité et la direction réelle des trades.
- [Archive Zenodo de la réplication microstructure](https://doi.org/10.5281/zenodo.19811426) — artefacts et fichiers de réplication associés au papier précédent.
- [Polymarket-v1 Database](https://arxiv.org/abs/2606.04217) — archive complète de trades on-chain et cycle de vie CTF sur plusieurs années ; utile pour construire des distributions historiques de liquidité, volume et règlements.
- [Dataset Polymarket-v1 sur Hugging Face](https://huggingface.co/datasets/TimeSeventeen/Polymarket-v1) — données en couches `OrderFilled`, `daily_aligned` et `CTF`, avec métadonnées normalisées et événements de résolution.
- [Unlocking the Forecasting Economy](https://arxiv.org/abs/2604.20421) — pipeline relationnelle qui relie métadonnées de marché, fills et événements oracle sur l’ensemble du cycle de vie Polymarket.
- [Projet Polymonitor](https://www.polymonitor.club/) — page projet associée au travail précédent ; à vérifier pour les schémas et mises à jour de données.
- [PMXT — archive gratuite de carnets](https://archive.pmxt.dev/Polymarket/v2) — snapshots horaires Parquet de carnets Polymarket et autres marchés ; utile pour un premier backtest, moins précis qu’un replay tick-level.
- [PMXT — code d’ingestion](https://github.com/pmxt-dev/pmxt) — bibliothèque et outils d’acquisition de données de marchés de prédiction.

### 6.2 Benchmarks de forecasting et de décision

- [Foresight Arena](https://arxiv.org/abs/2605.00420) — benchmark Polymarket on-chain avec commit/reveal, score de Brier et Alpha Score ; ce n’est pas le harness PredictionArena, mais il sépare proprement qualité de prévision et effets de sizing/timing.
- [Contrats Foresight Arena](https://github.com/foresight-arena/contracts) — implémentation open source du registre on-chain et du protocole commit/reveal.
- [ForecastBench — article](https://arxiv.org/abs/2409.19839) — benchmark dynamique de prévisions probabilistes, avec questions issues notamment de Polymarket ; utile pour valider le composant « forecasting » indépendamment de l’exécution.
- [ForecastBench — code](https://github.com/forecastingresearch/forecastbench) — pipeline, datasets et procédures de soumission ; utile pour les métriques Brier/calibration et les contrôles de contamination.
- [ForecastBench — présentation du benchmark](https://forecastbench.org/about/) — explique les questions de marché, les questions temporelles et le suivi dynamique.
- [Prophet Arena — article](https://arxiv.org/abs/2510.17638) — décompose le forecasting LLM en pipeline de collecte, raisonnement et scoring ; bon comparateur pour le sous-système de recherche/mémoire.
- [Prophet Arena — OpenReview](https://openreview.net/forum?id=Go9otu0U90) — version lisible, résultats et détails supplémentaires du benchmark.
- [Prophet Arena — recherche et ressources](https://prophetarena.co/research) — guides de leaderboard, stabilité de classement et articles complémentaires.
- [Beyond Forecasting: Belief-to-Trade Layer](https://openreview.net/forum?id=JgekLYbn9w) — distingue explicitement prévision, sélection, sizing, contraintes et exécution ; très utile pour concevoir une architecture où le modèle produit une croyance puis un module déterministe décide du trade.
- [Code de Raven-Agent](https://github.com/Alchemist-X/predict-raven) — référence open source de pipeline discovery → evidence → probabilité → classement → Kelly fractionné → contrôles de risque.
- [Bayesian Linguistic Forecaster](https://arxiv.org/abs/2604.18576) — mémoire de croyances structurée, mise à jour séquentielle, agrégation multi-essais et calibration ; utile pour remplacer ou améliorer la section de « critical learning » de PredictionArena.
- [FutureSim](https://openreview.net/pdf?id=s0F0l0Jl7e) — critique les évaluations live difficilement reproductibles et propose un replay d’événements ; utile pour tester les ablations de prompt, mémoire, recherche et harness.

### 6.3 Comparateurs pratiques de backtesting

- [PredictionMarketBench](https://github.com/Oddpool/PredictionMarketBench) — benchmark de backtesting avec replay de marchés Kalshi ; ce n’est pas Polymarket, mais la structure de simulation, de portefeuille et de métriques est directement réutilisable.
- [AI Trading Arenas](https://blog.flatcircle.ai/p/ai-trading-arenas) — synthèse secondaire des arènes de trading LLM, avec une description assez fidèle du contexte dynamique, des outils et de la mémoire de PredictionArena ; à utiliser pour repérer d’autres expériences, pas comme spécification normative.

## 7. Sources communautaires et traces publiques — priorité P3

- [Discussion Reddit sur PredictionArena](https://www.reddit.com/r/algotrading/comments/1qsgh6l/prediction_arena_7_ai_agents_trade_on_polymarket/) — observations d’utilisateurs sur les modèles, le nombre de trades, la divergence entre prix de marché et estimation IA et les comportements d’entrée/sortie.
- [Discussion Reddit sur PredictionArena](https://www.reddit.com/r/PredictionsMarkets/comments/1u11662/walkthrough-of-a-complete-ai-prediction-agent-on/) — exemple de walkthrough d’un autre agent Polymarket ; utile pour comparer les choix de découverte, de probabilité et de sizing, sans le confondre avec PredictionArena.
- [Question Manifold sur Claude Opus 4.6 et PredictionArena](https://manifold.markets/Simon74fe/will-claude-opus-46-be-profitable-o) — trace datée de l’arrivée de Claude Opus 4.6 et de l’existence de l’évaluation Polymarket.

## 8. À ne pas confondre avec le projet étudié

- [The Prediction Arena — nouveau site social](https://www.thepredictionarena.com/) — produit orienté jeux sociaux et compétitions de picks, alimenté par les données Polymarket. Le site n’est pas la documentation publique du benchmark LLM d’Arcada Labs décrit par le papier arXiv.

## 9. Checklist de reproduction à déduire de ces sources

Pour une reproduction expérimentale fidèle, l’implémentation devrait au minimum contenir :

- un synchroniseur Gamma/CLOB/Data et un cache local des marchés, événements, règles, tokens et statuts ;
- un discovery service avec recherche texte, tags, volume, liquidité, spread, volatilité et échéance ;
- un contexte de cycle versionné et horodaté, afin de conserver exactement ce que le modèle a vu ;
- une mémoire par agent : notes, croyances, plans, raisonnement précédent et historique court ;
- des outils séparés pour recherche Web, discovery, lecture du compte, achat/vente et gestion de mémoire ;
- un validateur déterministe : côté gagnant, statut de marché, solvabilité, frais, tick size, minimum order size et limite par marché ;
- un moteur d’exécution qui distingue FAK/FOK/limit orders, partial fills, rejet pour manque de contrepartie et paper trading ;
- un ledger append-only des ordres, fills, positions, prix d’entrée pondérés, sorties, règlements et raisons de rejet ;
- une valorisation cash + bid, avec snapshots de performance à chaque cycle ;
- des métriques distinctes : PnL réalisé/non réalisé, rendement, win rate de positions réglées, drawdown, Sharpe, turnover, usage des outils, tokens et durée de cycle ;
- un mode replay avec horodatage strict et interdiction des données postérieures à la décision ;
- des tests d’ablation : sans mémoire, sans web, sans discovery, sans raisonnement précédent, avec midpoint contre bid/ask, et paper contre exécution avec liquidité réelle.

## 10. Conclusion documentaire

La combinaison **papier arXiv + pages PredictionArena + documentation/SDK Polymarket + PolyBench** permet de reconstruire une réimplémentation solide du comportement observable. Elle ne permet pas de retrouver à l’identique les prompts propriétaires, les seuils internes, les paramètres de sampling ni la base de données privée. Ces éléments devront être estimés par expérimentation, par comparaison des traces historiques encore accessibles et par validation statistique sur des replays contrôlés.
