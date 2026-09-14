# Transports et vérification des accès

Les observations utilisent le compte forcé `ops-observer`, avec des clés
propres aux machines. Sur le poste de contrôle, le consommateur `opsreader`
utilise une configuration SSH fixe et vérifie les clés d’hôte. Le preflight
interroge seulement l’état systemd avec le collecteur autorisé.

Les opérations administratives déjà approuvées utilisent un autre consommateur,
`opsremoteadmin`. Les clés sont matérialisées par un service qui consomme le
coffre ; elles ne passent pas dans les arguments, les réponses aux agents ou
les journaux. Les copies de sauvegarde vérifient les empreintes de leur helper,
du récepteur et du transport. La migration conserve les versions précédentes
et leurs marqueurs dans une archive ; elle ne transforme pas un accord ancien
en autorisation d’une nouvelle opération.

La disponibilité SSH ne prouve aucun droit chez un fournisseur. Le runbook
`inventory.provider-access.v1`, de classe A, effectue des lectures bornées :
validité et règles de la clé OVH, lecture des zones DNS configurées chez
Infomaniak et accès au compte DeepSeek. Il ne modifie pas les DNS, ne commande
pas de serveur et ne lance pas de génération. Le rapport ne contient que les
statuts HTTP, des états et les règles d’accès ; jamais les clés ni le solde.

Son rôle OpenBao possède cinq références exactes. TLS est obligatoire, les
redirections sont refusées et les proxies provenant de l’environnement sont
ignorés. Les identifiants AppRole sont chiffrés avec les credentials systemd.

Une réponse OVH 200 sur l’identification, suivie de 403 sur DNS/VPS, indique
que cet accès ne permet pas ces opérations. L’audit expose les règles déclarées
pour distinguer ce cas d’une clé invalide. Un compte importé marqué comme
placeholder ne constitue pas un accès de secours.

La [documentation officielle OVH](https://docs.ovhcloud.com/en/guides/manage-and-operate/api/first-steps)
décrit l’association application/consumer key et les droits associés. Toute
nouvelle habilitation doit être accordée par le titulaire du compte. Les clés
sont ensuite saisies dans le coffre par un parcours privé ; elles ne doivent
pas être envoyées dans une conversation ou un dépôt Git.

## Installation et preuves

Les scripts de migration scellés décrivent une installation précise. Les
empreintes historiques doivent être réévaluées pour une autre installation ;
un export public anonymisé n’est pas une release signée de production.
Une installation du helper local ne vaut pas exécution d’une action distante.
Le broker et ses résultats font foi pour les opérations effectivement réalisées.
