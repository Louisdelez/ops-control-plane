# Découverte continue des inventaires fournisseurs

Le paquet isolé `provider-discovery/` prépare l’étape d’évolution continue du
catalogue. Il produit une photographie normalisée des identifiants publiés par
les fournisseurs et les compare exclusivement aux `deployment_mappings` du
registre. Une entrée distante absente des mappings est un **candidat à
examiner**, jamais un modèle approuvé ou activé.

## Limites de sécurité

- lecture stricte de `provider-integrations.v1` (champs inconnus, doublons JSON,
  références incohérentes et types invalides sont refusés) ;
- seuls `mode=api`, `method=GET`, `credential_scope=inference`, sans opération,
  avec une URL HTTPS littérale sont éligibles ; URL avec gabarit, requête,
  fragment, userinfo, IP littérale ou port autre que 443 refusée ;
- chaque nom est résolu une seule fois ; toute réponse DNS vide, malformée,
  mixte ou contenant une adresse non globale/non unicast est refusée, y
  compris les alias loopback, link-local, privés, multicast et les formes IPv6
  de transition qui encapsulent une IPv4 non globale ;
- la connexion TCP est épinglée à l’adresse validée sans seconde résolution,
  tandis que le certificat TLS et le SNI restent vérifiés avec le nom
  d’origine ; proxies désactivés, redirections refusées et pagination jamais
  suivie ;
- le magasin TLS est exclusivement le bundle Fedora fixe
  `/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`, vérifié root-owned et non
  inscriptible ; `SSL_CERT_FILE`, `SSL_CERT_DIR` et `SSLKEYLOGFILE` ne peuvent
  pas le remplacer ni activer un journal de clés ;
- échéance monotone totale de 10 s par fournisseur, couvrant DNS, connexion,
  négociation TLS, envoi et lecture même en flux lent ; réponse de 1 Mio,
  5 000 modèles, 25 000 nœuds JSON et profondeur 32 au maximum ;
- corps HTTP et credentials ne sont jamais intégrés au rapport ni aux erreurs ;
- aucune écriture de catalogue, base, manifeste, configuration ou secret.

Les schémas d’authentification actuellement exécutables sont `bearer`,
`x_api_key`, `google_api_key`, `api_key_or_bearer` quand son contrat est
explicitement `Authorization: Bearer`, et `x_api_key_and_version`. Pour ce
dernier, la clé suit le `header_name` du registre et le collecteur ajoute la
version de protocole Anthropic `2023-06-01`. Les autres schémas sont exclus avec
un code stable au lieu d’être devinés.

## Credentials par fichiers privés

Ni une clé en argument, ni une valeur d’environnement ne sont acceptées. Le
seul mécanisme est un fichier de correspondance privé, lui-même sans secret,
qui pointe explicitement vers des fichiers privés contenant chacun une clé
brute :

```json
{
  "schema_version": 1,
  "credentials": {
    "openai": {
      "credential_ref": "kv-infra-shared/data/llm/providers/openai",
      "path": "/run/credentials/openai-model-discovery"
    }
  }
}
```

Le `credential_ref` doit correspondre exactement au registre et chaque chemin
doit être absolu. Carte et fichiers de clés doivent être réguliers, non-liens,
appartenir à root ou à l’utilisateur courant et n’accorder aucun droit au
groupe/autres (par exemple `0400` ou `0600`). Une clé tient sur une ligne UTF-8
sans espace ni caractère de contrôle et mesure au plus 8 Kio.

## Installation et exécution

```bash
python3 -m venv .venv-provider-discovery
.venv-provider-discovery/bin/pip install ./provider-discovery
.venv-provider-discovery/bin/ops-provider-discovery \
  --registry /chemin/provider-integrations.v1.json \
  --credential-map /run/credentials/provider-discovery-map.json \
  > /chemin/prive/inventory.json
```

La sortie standard contient un objet JSON avec une ligne par compte fournisseur,
les candidats triés, leur classification `registered_mapping` ou
`unregistered_candidate`, les mappings enregistrés absents de la réponse et
les erreurs sous forme de codes bornés. `catalogue_mutated=false` et
`activation_performed=false` rendent le caractère consultatif explicite.

L’installation comme service, l’injection OpenBao/systemd, la périodicité et la
promotion contrôlée d’un candidat sont volontairement hors de ce composant.
