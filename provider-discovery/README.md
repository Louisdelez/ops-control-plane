# Collecteur d’inventaire des modèles fournisseurs

`ops-provider-discovery` est un composant Python autonome et installable. Il
interroge uniquement les contrats `model_discovery` littéraux dont le registre
`provider-integrations.v1` déclare exactement `mode: api` et `method: GET`.
Son rapport JSON est consultatif : il ne modifie ni le catalogue, ni une base,
ni une configuration et n’active aucun modèle.

Le transport résout chaque nom une seule fois, refuse toute réponse DNS
contenant une adresse non globale ou non unicast, puis épingle la connexion TCP
sur l’adresse validée tout en vérifiant le certificat TLS et le SNI avec le nom
d’origine. Il n’utilise ni proxy ni redirection. Une échéance monotone unique
couvre DNS, connexion, négociation TLS, envoi et lecture, y compris les réponses
en flux lent. La confiance TLS provient exclusivement du bundle Fedora fixe,
root-owned, et non des variables `SSL_CERT_FILE`, `SSL_CERT_DIR` ou
`SSLKEYLOGFILE`.

Voir [`../docs/provider-model-discovery.md`](../docs/provider-model-discovery.md)
pour le contrat de sécurité, le format des credentials et l’utilisation.
