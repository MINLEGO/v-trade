# Audit des endpoints PredictionArena / Polymarket

Dernière vérification : **13 juillet 2026**. Les requêtes ont été faites avec Python et les réponses ont été analysées en mémoire. Aucun JSON brut n’a été écrit dans le dépôt : les fichiers JSON associés sont des rapports structuraux ou des profils compacts.

Scripts reproductibles : [inspect_predictionarena_endpoints.py](../scripts/inspect_predictionarena_endpoints.py), [analyze_predictionarena_cycles.py](../scripts/analyze_predictionarena_cycles.py), [probe_predictionarena_pagination.py](../scripts/probe_predictionarena_pagination.py) et [profile_predictionarena_endpoints.py](../scripts/profile_predictionarena_endpoints.py).

## Résumé des réponses observées

Les tailles « décodées » sont celles du JSON non compressé et servent seulement à dimensionner l’ingestion.

| Endpoint | Enveloppe / cardinalité observée | Taille décodée | Pagination observée | Utilité pour V-Trade |
|---|---|---:|---|---|
| `/agents` | objet `{count, data}` ; 10 agents | 8 Ko | non testée | registre et statistiques agrégées |
| `/account-value-history` | objet `{data}` ; 19 878 points horaires | 3,3 Mo | aucune | métrique historique, faible priorité |
| `/external-markets-history` | objet `{SP500}` ; 632 points | 53 Ko | aucune | série externe de comparaison, nom de série à confirmer |
| `/cycles?offset=0&limit=50` | objet `{count, cycles, hasMore}` ; 50 cycles | 4,0 Mo | `offset`/`limit` fonctionnent ; `limit=100` renvoie 100 cycles | **source la plus riche : prompt, raisonnement, tool calls, états avant/après** |
| `/actions?offset=0&limit=50` | tableau ; 50 actions | 52 Ko | `offset`/`limit` fonctionnent | journal d’exécution synthétique |
| `/markets` | tableau ; 15 marchés actifs, 2 outcomes chacun | 30 Ko | aucune | liste de présentation ; insuffisante pour un replay |
| `/agents/{id}/actions` | tableau ; 93 actions pour Claude, 55 pour GPT-5.4 | 97 Ko / 53 Ko | `limit=1` est accepté, sans enveloppe `count`/`hasMore` | actions récentes par agent |
| `/agents/{id}/positions-with-prices` | tableau ; 38 positions Claude, 1 GPT-5.4 | 21 Ko / 0,6 Ko | aucune observée | portefeuille courant marqué au prix courant |
| `/agents/by-model/{model}` | objet de profil | 27 Ko | aucune | statistiques, croyances et plans persistants |
| `/agents/{id}/settlements` | tableau ; 100 règlements Claude | 59 Ko | `limit=1` accepté | résultats et P&L réalisés |

Le paramètre `count` de `/cycles` correspond au nombre renvoyé par la page observée : il vaut 1 avec `limit=1` et 100 avec `limit=100`. Il ne faut donc pas le traiter comme un total global. `hasMore` était `false` dans ces tests, y compris pour `limit=100`.

## Schémas utiles

### Agents et valeur du compte

`/agents` renvoie notamment :

```text
agent_id, model_id, status, started_at, completed_at,
account_value, latest_account_value,
cash_balance, latest_cash_balance,
total_pnl, realized_pnl, return_percentage,
number_of_trades, total_bets_count, total_cycles,
win_rate, sharpe_ratio, max_drawdown,
biggest_win, max_win, max_loss,
max_settlement_win, max_settlement_loss
```

Il faut conserver séparément les champs `account_value`/`latest_account_value` et `cash_balance`/`latest_cash_balance` : l’API expose manifestement un état agrégé et un état plus récent dans la même ligne.

`/account-value-history` est plus simple :

```text
agent_id, model_id, date, value, cash, status
```

La période observée allait du 9 février au 24 juin 2026. L’endpoint contient beaucoup de points mais n’apporte pas le contexte qui explique les changements de valeur ; il doit rester une série de performance, pas une source de décision.

`/external-markets-history` ne renvoie pas un tableau générique mais une clé de série explicite :

```text
{"SP500": [{"date": ..., "value": ..., "return_percentage": ...}]}
```

La série observée allait du 13 juin au 13 juillet 2026. Le nom `SP500` est clair, mais l’échelle de `value` et la source exacte doivent être enregistrées comme métadonnées externes plutôt que supposées.

### Cycles : le meilleur point d’entrée

Chaque élément de `cycles` contient :

```text
id, cycle_id, agent_id, model_id, agent,
created_at, updated_at, completed_at,
status, cycle_duration_ms, thinking_duration_ms,
initial_account_value, initial_cash_balance,
initial_open_orders, initial_positions,
post_account_value, post_cash_balance,
post_orders, post_positions,
prompt, reasoning, research_data, settlements, tool_calls
```

Les positions dans les snapshots de cycle ont un format différent et plus riche que celui de `positions-with-prices` :

```text
asset, conditionId, eventId, eventSlug, slug, title,
outcome, outcomeIndex, oppositeAsset, oppositeOutcome,
avgPrice, curPrice, size, initialValue, currentValue,
cashPnl, realizedPnl, percentPnl, percentRealizedPnl,
endDate, redeemable, mergeable, negativeRisk, proxyWallet,
totalBought, icon
```

La plupart des cycles observés contenaient un prompt de 9 012 à 27 904 caractères, médiane 13 174. La propriété est un unique champ `prompt` : l’endpoint ne fournit pas de messages séparés avec rôles `system`, `user` et `assistant`. Il faut donc le stocker comme `rendered_cycle_prompt`, sans le renommer abusivement en prompt système.

### Prompt rendu et méthodologie observable

Les lignes communes du prompt montrent une procédure très explicite :

1. choisir entre trade fondamental et trade de mouvement de prix (« market-making ») ;
2. rechercher avec discovery, détails de marché, carnet et `web_search` ;
3. écrire la condition gagnante de YES et de NO avant chaque trade ;
4. calculer probabilité, prix cible, edge et P&L attendu après frais/gas ;
5. dimensionner, fixer un plan de sortie et exécuter via l’outil de trading.

Les rappels visibles couvrent le P&L, l’incertitude, les scénarios disconfirmants, les règles de résolution, les prix inférieurs à 10 cents, le risque de timing, le choix du côté et la profondeur du carnet. Le prompt contient aussi l’état courant, les ordres ouverts, les positions, les croyances/plans et la gestion des connaissances.

Deux précautions sont importantes :

- la chaîne contient littéralement `{trading_tool_ref}` dans certaines instructions communes ; le prompt exposé est donc un rendu partiellement templatisé ou conserve un placeholder ;
- les lignes de protocole apparaissent 55 fois dans l’analyse de 50 cycles, ce qui suggère que le champ peut contenir des blocs répétés ou concaténés. Il faut conserver le texte exact avant toute normalisation.

### Tool calls

`cycles[].tool_calls` est un tableau d’objets :

```text
arguments, call_id, category, display_name,
output, success, timestamp, tool_name
```

Sur les 50 cycles inspectés :

- 849 appels au total ; médiane de 10,5 appels par cycle, maximum 92 ;
- catégories : 763 `discovery`, 44 `knowledge`, 42 `trading` ;
- tous les appels observés avaient `success=true`, y compris dans des cycles dont le statut global était `failed`.

Noms observés, regroupés par fonction :

| Fonction | Tool names observés |
|---|---|
| Recherche / discovery | `discover_hot_markets`, `discover_by_time_remaining`, `discover_events`, `list_top_events`, `get_market_details`, `web_search`, `get_orderbook`, `browse_markets_by_volume`, `discover_by_price_volatility`, `get_event_markets`, `get_newest_events`, `get_all_active_markets`, `discover_by_volume_trend`, `discover_by_competitive_score`, `discover_by_date_range`, `search_tags` |
| Compte / historique | `get_balance`, `get_portfolio`, `get_open_orders`, `get_closed_trades`, `get_settlements` |
| Mémoire / plans | `get_general_beliefs`, `search_general_beliefs`, `create_general_belief`, `delete_general_belief`, `create_long_term_plan`, `get_next_cycle_plan`, `create_next_cycle_plan` |
| Trading | `place_market_order` |

Exemples de formes d’arguments observées :

```text
discover_events(keyword, limit, min_liquidity?, min_volume_24hr?)
discover_hot_markets(hours_back?, limit, min_liquidity?, min_volume_24hr?)
discover_by_time_remaining(hours_min?, hours_max, limit, min_liquidity?)
get_market_details(slug)
get_orderbook(token_id)
web_search(query)
create_general_belief(belief_content, category, confidence)
create_next_cycle_plan(plan_content, cycle_date?)
place_market_order(token_id, side, amount, conviction?)
```

Ce sont des formes déduites des appels réellement journalisés, pas les JSON Schemas complets. Les réponses de l’API ne révèlent ni toutes les propriétés optionnelles, ni les enums exhaustifs, ni les limites d’autorisation. V-Trade doit donc versionner ses propres schémas canoniques et marquer cette partie `inferred`.

### Nombre de recherches Web par cycle

Une analyse dédiée de `cycles?offset=0&limit=200` a compté les tool calls dont `tool_name == "web_search"`, sans conserver les prompts ni les résultats de recherche. Les 200 cycles ont été renvoyés ; le maximum observé est de **25 appels `web_search` dans un cycle** :

```text
cycle : trading-glm-5-cycle-20260624-142702
modèle : glm-5
statut : completed
web_search : 25
tool calls total : 61
```

Dans cet échantillon, 88 cycles n’avaient aucun `web_search`, 112 en avaient au moins un, la moyenne était 4,085 et la médiane 1. Cette valeur de 25 est une borne empirique sur les 200 cycles observés, pas un plafond documenté : le système pouvait éventuellement autoriser davantage dans d’autres cycles ou une autre période. Le rapport compact est [predictionarena-web-search-200-cycle-analysis.json](predictionarena-web-search-200-cycle-analysis.json).

### Actions et marchés

Une action contient :

```text
id, cycle_id, agent_id, model_id,
action_type, side, amount, filled_size, price, total_cost,
market_slug, market_title, event_slug, ticker, outcome,
status, error_message, action_result,
is_realized, settlement_status, timestamp, created_at
```

`action_result` expose notamment `order_id`, `token_id` et `tool`. Dans les exemples observés, les ordres sont explicitement des ordres papier (`paper-...`) et l’outil est `place_market_order`.

L’endpoint `/actions` est une vue d’activité, pas un ledger canonique : la coexistence de `amount`, `filled_size`, `price` et `total_cost` ne suffit pas à déduire sans ambiguïté la convention de quantité/coût. Pour le replay, il faut privilégier les tool calls, les snapshots de cycle et un ledger local validé.

`/markets` est beaucoup plus pauvre :

```text
id, market_id, condition_id, slug, question, description,
outcomes, clob_token_ids, active,
start_date, end_date, created_at, updated_at, icon, image
```

Il ne contient pas les prix, le spread, la profondeur ni les paramètres complets du carnet. Il ne doit pas remplacer Gamma/CLOB dans l’adaptateur Polymarket de V-Trade.

### Positions et règlements

`positions-with-prices` expose :

```text
id, ticker, market_slug, market_title, token_id,
side, quantity, average_entry_price, current_price,
position_value, unrealized_pnl, updated_at
```

Les positions Claude observées étaient principalement `YES` (33) et `NO` (5). Le champ `current_price` est un prix de valorisation présenté par le site ; il faut encore vérifier s’il correspond toujours à un bid exécutable ou à une autre convention.

`settlements` expose :

```text
id, ticker, market_slug, market_title, token_id,
side, outcome, quantity, payout, settlement_amount,
realized_pnl, result, created_at, settled_at
```

Attention au replay : `settled_at` peut être postérieur à `created_at` et peut même être postérieur à la date d’observation d’un cycle. Il faut appliquer une coupure temporelle stricte et ne pas charger aveuglément les 100 derniers règlements.

### Profil détaillé d’un agent

`/agents/by-model/claude-opus-4-6` ajoute aux statistiques :

```text
general_beliefs: [{belief_content, category, confidence}]
long_term_plan: {created_at, plan_content, target_date}
next_cycle_plan: {created_at, cycle_date, plan_content}
```

Dans l’échantillon, il y avait 34 croyances réparties entre `event_analysis`, `risk_assessment`, `market_structure` et `trading_strategy`, ainsi que des plans de 2,5 Ko environ. C’est une preuve directe que la mémoire est persistante, structurée et propre à l’agent ; ce n’est pas seulement un résumé calculé à partir des positions courantes.

## Conséquences pour le plan V-Trade

1. **Ingestion sélective.** Archiver `cycles` et ses tool calls comme artefact brut compressé, mais ne pas injecter les 4 Mo dans les logs ou le contexte modèle. Indexer séparément les métadonnées de cycle, les noms/arguments de tool calls, les états avant/après et les hashes des prompts.
2. **Prompt.** Stocker le texte exact sous une version de prompt rendu. La séparation system/user, la liste des définitions d’outils et les paramètres de sampling restent inconnus ; ils doivent rester des décisions `inferred` dans `experiment_definitions`.
3. **Outils.** Implémenter des interfaces provider-neutral correspondant aux noms observés, mais ne pas prétendre avoir retrouvé les schemas internes. Les champs `tool_name`, `arguments`, `output`, `success` et `timestamp` deviennent le minimum d’audit.
4. **Comptabilité.** Ne pas utiliser `/actions` comme source financière unique. Reconstruire le ledger depuis les intentions validées, les fills et les settlements de V-Trade, puis utiliser cette API comme fixture de comparaison.
5. **Reproductibilité.** Les données affichées sont un snapshot de l’application : les cycles inspectés s’arrêtent au 24 juin alors que la série SP500 va jusqu’au 13 juillet. Chaque artefact doit donc porter son `checked_at`, son cutoff de données et son hash.
6. **Fiabilité.** Enregistrer séparément le statut du cycle et le statut des tool calls : dans l’échantillon, 18 cycles sur 50 étaient `failed`, malgré 849 appels tous marqués réussis. La cause de l’échec global n’est pas fournie dans ce schéma.

## Inconnues qui restent ouvertes

- prompt système original et séparation des rôles de messages ;
- schemas JSON complets, limites, enums et règles d’autorisation des tools ;
- seuils internes de discovery et plafond de dépense par cycle ;
- cause des cycles `failed` et politique de retry/timeouts ;
- convention exacte de `current_price`, `amount`, `filled_size` et `total_cost` ;
- provenance et échelle de la série `SP500` ;
- relation exacte entre les snapshots du site et les APIs Gamma/CLOB/Data officielles ;
- algorithme de génération/mise à jour des croyances, plans et « critical learning ».

Rapports compacts générés par Python : [endpoint report](predictionarena-endpoints-report.json), [cycle analysis](predictionarena-cycle-analysis.json), [pagination probe](predictionarena-pagination-probe.json) et [endpoint profile](predictionarena-endpoint-profile.json).
