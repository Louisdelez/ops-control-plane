# Catalogue multi-modèles

`model-catalog.v2.json` est le registre public et non sensible partagé par
l'orchestrateur et l'application Tauri. Il sépare volontairement :

- une carte de modèle (`cards`) ;
- un compte fournisseur et sa référence OpenBao (`provider_accounts`) ;
- un déploiement réellement revu (`deployment_ids`) ;
- une ou plusieurs grilles de prix (`pricing`).

Une clé appartient au compte fournisseur, jamais à une copie de chaque modèle.
L'interface peut donc proposer le bouton de configuration sur chaque carte tout
en faisant pointer toutes les cartes Alibaba, par exemple, vers la même
référence opaque. Aucune valeur de secret ne figure dans ce catalogue.

Le registre contient exactement 58 cartes et 21 comptes fournisseur. Depuis la
revue du 5 septembre 2026, 48 cartes ont au moins une grille tarifaire et 11
sont en quarantaine. Aya Expanse 8B reste non routable après son retrait par
Cohere le 4 avril 2026, statut provenant exclusivement de la documentation
officielle Cohere référencée dans `provider-integrations.v1.json`. Le TXT ne
déclare pas ce retrait ; il fournit conjointement le tarif historique des
variantes 8B et 32B, conservé à l'identique sur les deux cartes. Il ne
confond jamais les quatre niveaux suivants : présence de la carte, déclaration
d'un déploiement, résolution locale de sa clé et succès d'un canari réseau
facturable. Seul le premier niveau est garanti par ce fichier public.

La répartition d'intégration est exacte : **5 cartes `configured`**, qui portent
**7 identifiants de déploiement rattachés au catalogue**, **42 cartes
`catalogued`** sans déploiement et **11 cartes `quarantined`**. La configuration
runtime contient aussi trois voies explicitement auxiliaires et hors catalogue :
`qwen-coder-api`, dont l'identifiant exact Flash ne correspond à aucune des
trois cartes Qwen Coder du seed, puis les backups génériques et pilotés par
environnement `embedding-api-backup` et `reranker-api-backup`. Aucun de ces
trois identifiants ne change silencieusement l'état d'une carte Atlas.

## Import du 5 septembre 2026

Le seed fourni contient 59 entrées. La normalisation produit 58 cartes :

- les entrées DeepSeek V4 Flash heures creuses/pleines sont une carte avec deux
  schedules ;
- les entrées DeepSeek V4 Pro sont traitées de la même façon ;
- la ligne Aya Expanse 8B/32B devient deux cartes distinctes ;
- les dix familles sans variante et prix exacts restent visibles avec l'état
  `quarantined`.

Les 48 cartes initialement tarifées sont toutes importées. La carte Aya Expanse
8B conserve son prix historique issu du TXT, sans que celui-ci ne la rende
routable ni ne soit présenté comme un tarif actif vérifié. Son statut de retrait
possède la provenance Cohere distincte décrite ci-dessus.
`configured` signifie
uniquement qu'un profil de déploiement correspondant existe dans la
configuration source. Cela ne signifie ni qu'une clé est présente, ni que le
provider est actif, ni qu'un canari a réussi. Ces états doivent venir du runtime.
Les dix familles non résolues et Aya Expanse 8B restent en quarantaine,
visibles mais non routables.

`provider-integrations.v1.json` complète ce catalogue avec les contrats
d'intégration vérifiés : protocoles, régions, découverte des modèles,
identifiants d'inférence et d'administration séparés, ainsi que six capacités
financières typées. `balance_mode` reste présent dans le catalogue v2 pour la
compatibilité des lecteurs existants ; `financial_capabilities` est la source
structurée à utiliser désormais.

## Scores et prix

Les profils 0–10 sont des *priors éditoriaux* à faible confiance, dérivés des
descriptions du fichier. Ils ne sont pas des benchmarks. Les futures mesures
internes doivent être conservées séparément avec leur nombre d'échantillons.

Le scénario commun correspond toujours à 10 M tokens d'entrée et 2 M de sortie,
mais précise 1 000 appels de 10 000 + 2 000 tokens. Cette distribution rend les
paliers par requête reproductibles. Aucun tarif sans source officielle, région,
date d'effet et contrat d'API validé ne doit devenir routable automatiquement.

Le tarif d'une carte décrit sa source de catalogue. Lorsqu'un même modèle est
servi par plusieurs hébergeurs, le coût d'un déploiement runtime est recalculé
avec le tarif conservateur du provider qui l'héberge. La prévisualisation
runtime compare ces coûts de déploiement ; elle ne réutilise pas aveuglément le
prix de la carte primaire. Les champs `cost_basis`, `selected_deployment_id` et
`selected_provider_account_id` rendent cette distinction explicite.

Chaque carte expose aussi explicitement `context_window_tokens`, `languages`,
`limitations`, les latences p50/p95, la confiance et la source de ses
métadonnées techniques. Une valeur absente du seed reste `null` ou une liste
vide : elle n'est jamais déduite de la marque, de la taille ou d'un palier
tarifaire. Le TXT ne donne une fenêtre numérique de 1 M tokens que pour
DeepSeek V4 Flash, Gemini 3.1 Flash-Lite et Grok 4.20. Il ne fournit aucune
latence mesurée. Chaque grille de prix porte sa propre confiance, sa source et
un champ de prix cache. Le seed compare explicitement les tarifs hors cache ;
un prix cache non nul n'est donc accepté qu'avec une provenance officielle
séparée, explicite et validée.

À ce jour, le runtime source relie 5 cartes statiques et génère 9 liaisons
supplémentaires revues via Z.ai, OpenAI Responses et Moonshot. Cela porte à 14
sur 58 les cartes ayant un contrat d'invocation, pour 16 déploiements catalogue ;
les 44 autres sont bloquées avec un motif explicite. Une liaison, un emplacement
de credential ou un ID vérifié ne vaut ni clé présente, ni activation, ni canari
réseau réussi.

Validation locale :

```bash
python3 scripts/validate-model-catalog.py catalog/model-catalog.v2.json \
  --source catalog/sources/catalogue_modeles_IA_API_2026.txt

python3 scripts/validate-provider-integrations.py \
  catalog/provider-integrations.v1.json \
  --catalogue catalog/model-catalog.v2.json
```

Le seed fourni est archivé sans modification sous `catalog/sources/`. Son
SHA-256 doit rester `179dabf94c731541e929fe57845fedff79f249af058f483fcc4146d9c8b0d62e` ;
la validation refuse toute divergence entre cette preuve et le catalogue.
