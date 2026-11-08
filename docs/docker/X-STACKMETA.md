# x-stackmeta

`x-stackmeta` is a custom top-level Compose extension for descriptions, autostart intent
dependencies between whole stacks, stack labels and reusable definitions. YAML and JSON Compose definitions are supported, including stored structured definitions. A stack containing this
section is shown as of type `compose-meta`.

Current support includes discovery, validation, state assessment and explicit
manual controls. `autostart: true` records intent; no autonomous startup scheduler
runs at daemon startup. Eligibility does not mean required states are satisfied.

## Definition

```yaml
name: application
services:
  web:
    image: example/web:latest
    restart: "no"

x-stackmeta:
  autostart: true
  description: "Web application with database and migration prerequisites"
  dependencies:
    requires:
      - database
      - stack: redis
        state: healthy
      - stack: migration
        state: completed
```

| Property | Type | Meaning when omitted |
| --- | --- | --- |
| `autostart` | Boolean | `false`; no automatic-start intent |
| `description` | String | Empty description |
| `labels` | Mapping or list of strings | No stack labels |
| `template` | Stack name | Independent definition |
| `overlay` | JSON Merge Patch object | No explicit patch |
| `dependencies` | Object containing `requires` | No requirements |
| `dependencies.requires` | Stack name or list | No required stacks |

An empty `x-stackmeta: {}` section is valid. Unknown properties and invalid value
types are reported. Docker Compose ignores extension fields prefixed with `x-`;
see its [extension documentation](https://docs.docker.com/reference/compose-file/extension/).

## Dependency forms

```yaml
dependencies:
  requires: database
```

```yaml
dependencies:
  requires: [database, redis]
```

```yaml
dependencies:
  requires:
    - database
    - stack: redis
      state: healthy
    - stack: migration
      state: completed
```

A plain name is equivalent to `{stack: name, state: healthy}`.

| State | Condition |
| --- | --- |
| `started` | Required containers are running, not paused or restarting. |
| `healthy` | Defined healthchecks report healthy; services without healthchecks use `started`. |
| `completed` | Job containers exited successfully with exit code zero. |

`ready` is not supported. Conditions use Docker evidence; missing health/exit
information remains unknown. Declared replica counts are respected when
available; otherwise one container per assessed service is required. The newest
relevant containers are assessed so replaced jobs do not permanently block a
successful run. Optional profiles without discovered containers are not treated
as active services. Without a readable definition, discovered containers cannot
prove that all intended services exist. Swarm dependency assessment can remain
unknown because local containers do not establish cluster-wide health.

Missing, ambiguous, cyclic or failed requirements are highlighted in red. Other
entries use the required stack's runtime state color and state whether the
requested condition is satisfied. A running stack can still fail `healthy` or
`completed`; runtime color alone is not a satisfied dependency.

## Restart-policy exclusion

Every service must omit automatic restart or use `restart: "no"`. Any other
restart policy excludes the whole stack from extended startup because Docker
restarts can bypass cross-stack ordering. A specified `deploy.restart_policy`
must use `condition: none`.

The metadata box identifies conflicting services and marks startup/dependency
metadata invalid. Description remains visible. An unsupported `restart` property
under `x-stackmeta` is invalid too. The assessment does not change restart
policies. Unresolved interpolation, include/extends or inaccessible definitions
do not establish eligibility; resolve the configuration before relying on it.

## Finding and editing definitions

Source labels supply file paths and working directories. Conventional filenames
include `compose.yaml`, `compose.yml`, `compose.json`, `docker-compose.yaml`,
`docker-compose.yml` and `docker-compose.json`. Optional explicit search
directories can locate additional local projects. Access is bounded by configured
allowed roots and actual mounts/permissions; remote Engine paths are not local
file evidence.

Metadata assessment is not a complete Compose deployment resolver. It does not
execute environment interpolation, `.env`, include/extends or every Compose merge
rule. For multiple saved sources, later values can replace earlier assessed
metadata. Unreadable or unsupported sources produce warnings rather than valid
empty metadata.

See [stack files and controls](stacks.md) for creation, durable drafts, source
permissions, confirmed saves, structured conversion and lifecycle operations.
Manual Start/Restart checks eligible required conditions using saved definitions;
Stop does not recursively stop dependencies. See the
[daemon environment tables](../backend/README.md#stack-definitions-source-paths-and-controls)
and [Compose mounts](../deployment/README.md#daemon-mounts) for deployment paths and tool needs.

## Stack labels

Labels belong to the stack itself, independently of service/container labels.
Use the same mapping or list notation as Compose labels:

```yaml
x-stackmeta:
  labels:
    org.example.environment: production
    org.example.owner: operations
```

The equivalent list is `["org.example.environment=production", "org.example.owner=operations"]`.
A bare key or null mapping value means an empty string; duplicate list keys use
the last value. Keys must be nonempty and must not use the reserved
`com.docker.compose.` prefix. Quote boolean values; nested values are invalid.
The Labels page counts stacks separately and links to the stack list using
`label.key` and `label.value` filters.

## Templates and overlays

Templates are ordinary stack definitions, addressed by stack name. A derived
stack can omit services and inherit a definition:

```yaml
name: application
x-stackmeta:
  template: base-application
  overlay:
    services:
      web:
        image: example/web:v2
      obsolete-worker: null
    x-stackmeta:
      labels:
        org.example.environment: staging
```

Child fields override inherited fields recursively. Explicit `overlay` uses JSON
Merge Patch semantics: mappings merge, null removes a property, and arrays replace
the entire array. This is the application overlay behavior, not Docker Compose's native
multi-file merge syntax. Use the overlay to remove inherited properties.
References may form chains; missing references, cycles and excessive depth make
the definition unavailable for deployment. An overlay cannot redefine its own
`template` or `overlay` directives. Derived stacks retain their own source origin.

All top-level Compose fields are retained in the structured model, including
networks, volumes, configs, secrets and extensions. Reading a file does not run
interpolation or resolve every native Compose feature. The Compose command
normalizes the reviewed deployment configuration before execution.

After explicit initialization, the definition tree offers **Duplicate**, **Derive**
and **Edit**. Confirmation pages compare the affected definition or the selected
Active version with the captured running baseline. Internal patches preserve
explicit nulls as well as removals. See [stack management](stacks.md#definition-tree).
