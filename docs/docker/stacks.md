# Stack files and controls

## Create a stack

Choose **Add stack** from a stack-list toolbar, enter a free name and choose an
input method. Upload one YAML/JSON Compose file or a ZIP/TAR bundle, or paste text
and start a new definition. Stack Builder and Stack Templates are planned choices
and do not create stacks yet.

Review file names, sizes and hashes, then confirm. Creation never starts or deploys
containers. Names must be free in Docker, stored definitions and the managed
directory. Each new stack has a dedicated `BACKEND_COMPOSE_ROOT/<name>` directory.

| Limit | Value |
| --- | --- |
| Files per bundle | 64 |
| Total uploaded/extracted content | 4 MiB |
| Main Compose definition | 1 MiB |
| Upload confirmation lifetime | 10 minutes |

Paths must stay inside the new stack directory. Links, devices, traversal,
duplicate paths and conflicting entries are rejected. Select the main definition
when needed; other files retain their relative paths. Creation does not replace
an existing directory. Uploaded bundles must be uploaded again after an expired
preview; pasted text uses per-user drafts.

## Existing Compose sources

Local project labels identify configuration files and working directories. Source
access is limited to `BACKEND_COMPOSE_ALLOWED_ROOTS`; optional explicit search
directories find additional projects. A remote Engine's project paths cannot be
treated as local files. Missing/unreadable sources produce warnings and do not
become empty, valid definitions.

After a filesystem read failure, the daemon probes the source path and its
ancestors. Diagnostics identify the missing or inaccessible component, such as
`Path does not exist: /root` or `No access to /root`, using host paths even when
the host filesystem is mounted under a container prefix. Parser errors do not
include source contents.

In containers, configure source-root mapping and actual directory mounts together.
Managed Compose paths should be identical on the host and inside the daemon so
relative bind sources resolve correctly for the host Engine. External sources
default to read-only even with Docker write access. See
[daemon source settings](../backend/README.md#stack-definitions-source-paths-and-controls)
and the [mount table](../deployment/README.md#daemon-mounts).

## Editor and drafts

Stack details contain a **Compose definition** box with source type, working
directory, source paths and explicit editor/conversion actions. Source filenames
are plain text; use the editor button to open them.

Text edits are saved as per-user drafts. Leaving the page, reconnecting or live
inventory refresh does not silently replace the draft. **Save draft** forces a
flush. Drafts can contain secrets, so protect the web database and backups.

Writing a file requires administrator Docker write access, a current password,
matching OS owner/group/write permissions and a writable mount. Review the diff
and confirm. The source hash is checked again; a changed file requires review of
the current source before retrying. File mode and ownership are preserved.
A successful save clears only the draft revision that was reviewed, preserving
newer edits from another tab. Lifecycle controls use saved definitions, not drafts.

## Database definitions

Use **Duplicate** to make an independent database copy of a definition, or
**Derive** to create a child that inherits its parent. File-backed definitions are
parsed to structured Compose data automatically; no separate conversion action
is needed. Source files remain unchanged. Definition names describe versions;
the stack name remains the single project identity for all of them.

Database definitions preserve the complete Compose structure. The editor uses
JSON and retains per-user text drafts. Source entries open the original YAML/JSON
file editor, respecting its existing ownership and read-only rules. Captured
historical sources, Running definitions and definitions with dependent children
open read-only. Duplicate or derive them before editing.

## Start, stop and restart

Stack lists, details and editors expose control buttons when write access permits
them. A preview identifies the operation and saved configuration, followed by an
explicit confirmation. Status and output appear while the job runs.

File-backed Compose controls need the configured Compose command inside the daemon
and suitable path mapping/mounts. If saved Compose sources cannot be resolved,
existing containers may offer an Engine-API fallback. Its preview identifies the
fallback; it cannot create missing resources. Incomplete runtime inventory blocks
operation on a partial set. Swarm operations require an appropriate manager.

When the selected Active node differs from Running, Restart recreates containers
using the reviewed configuration. Apply uses Compose up to reconcile the saved
definition. Stop does not recursively stop required stacks. Extended metadata
checks dependency conditions before manual Start/Restart when eligible; it does
not imply a general startup scheduler. See [x-stackmeta](X-STACKMETA.md).

## Initialize stack management

Discovered stacks are readable without being managed. Open a stack and choose
**Initialize stack management** in its **Compose definition** box. This creates
one root entry immediately, without an intermediate confirmation. Its name is the
original Compose path, or the stack name for a database source. Initialization
changes neither files nor containers. Reopening the stack restores its management
by the globally unique stack name; another stack cannot reserve that name.

The running source observed during discovery cannot prove exactly what an
external deployment originally used. The Running badge's tooltip identifies such
observations. A successful the application deployment records the reviewed definition. When
an existing baseline cannot be associated with an entry, the running definition
remains unknown rather than being assigned to a different version.

## Definition tree

The **Compose definition** box shows definition names and their hierarchy, with
**Active** and, while containers run, **Running** markers. It contains no separate
origin/type summary or standalone management-data viewer. Review diffs appear
when confirming changes or applying a saved definition.

| Action | Effect |
| --- | --- |
| **Edit** | Open exactly this definition. Read-only sources and protected snapshots remain read-only. Unused database leaves can be edited and renamed after review. |
| **Duplicate** | Enter a name, initially suggested as the original name plus ` (copy)`, then review an independent database copy at the same indentation level. A copy of a read-only file needs no write access to that file. |
| **Derive** | Create a child definition referring to this parent, with a default name based on the current UTC date/time. |
| **Set Active** | Select the definition for the next deployment after review. The button appears only on inactive entries. |
| **Delete** | Remove an eligible unused database leaf after confirmation. File origins, Active/Running entries and entries with children are protected; the button is absent for them. |
| **New Definition** | Use upload/import or text/empty input to create another independent root for the same stack. The available Stack Builder and Stack Templates choices follow the existing creation workflow. |

New copies, derived versions and independent roots do not change Active and do
not start containers. Select **Set Active** explicitly. Start/Apply use Active;
Restart applies it when it differs from Running. Successful deployment moves
Running to the reviewed entry. Failed deployment does not move it. Selecting
another version while a job runs cannot change that job's reviewed configuration.

The parent of an inherited definition and the Running snapshot remain fixed.
File edits create source revisions, preserving earlier captured definitions. When
Active followed the previous source it follows the new revision too; a selected
copy or derived version is not silently rebased. Creating a derived version from
a newer source lets you review the adjustments against that source.

Below the tree, a running stack shows its definition name, working directory,
creation time and start time. Times come from the Engine: creation is the earliest
creation of the current containers, start is the latest start among running
containers. Missing evidence is shown as unknown.

## New definition inputs and resources

**New Definition** opens the same upload/paste workflow used for new stacks, while
keeping the existing stack name. A definition name identifies the new root. Upload
one YAML/JSON file or a ZIP/TAR archive with auxiliary files, paste Compose text,
or start with an empty definition. Builder/Templates remain planned choices.
Review file hashes and the resulting definition before confirming. Conversion to
structured Compose data happens internally.
The review shows the new standalone definition, without comparing it with Active
or Running.

Imported resources receive a dedicated daemon-owned directory within the stack's
managed workspace. Relative resource paths retain their context. Creating a new
version never replaces the external origin, reserves another project name or
starts containers. Deleting a database definition does not remove imported files
or runtime data referenced by them.

The existing bundle limits apply: at most 64 regular files and 4 MiB total, with
a main Compose definition up to 1 MiB. Archives cannot contain links, traversal
or special files. Source-file saves retain owner/mode and verify content hashes.
All management edits require administrator Docker write access, a current password
and CSRF protection. Initialization runs directly and checks the source hash;
other changes require review/confirmation and an unchanged management revision.
A successful save clears only its reviewed draft revision; newer drafts survive.
History is limited to 128 definitions and 16 MiB per stack. Protect the daemon
state and web draft databases because definitions can contain secrets.

Prefix-mapped projects can normalize definitions through the read-only host mount.
Relative service bind paths are translated back to their original host paths for
the Engine. File-backed Compose secrets/configs still require project directories
mounted at their original host paths. Builds and image pulls are not performed by
these stack controls.
