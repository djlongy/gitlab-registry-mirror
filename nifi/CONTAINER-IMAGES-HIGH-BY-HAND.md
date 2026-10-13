# Build the container-images high-side flow by hand

A NiFi 2.x flow for the high side of the diode. It takes each reassembled file,
keeps only `container-images-*.tar.ffv3`, unpacks it, checks its attributes and
its checksum, and writes it into the inbox that `mirror.py import` reads. Anything
not listed stays at its default.

You build it in five parts: parameters, process group, ports, 9 processors, 13
connections. Property names, parameter names and relationship names are
case-sensitive: type them exactly as written.

## What arrives

The low pipeline (`MIRROR_SEND_FLOWFILE=true`, `MIRROR_SEND_PATH` on the NFS share)
writes one file per export: `container-images-<pipeline id>.tar.ffv3`, or
`container-images-<pipeline id>-001-of-003.tar.ffv3` and so on with
`MIRROR_MAX_SIZE`. Each is a NiFi FlowFile v3 package holding one FlowFile: the tar,
with these attributes, names exactly as written:

| Attribute | Value |
|---|---|
| `filename` | the tar's name, e.g. `container-images-11268.tar` |
| `Filename` | the same |
| `Content-Type` | `application/octet-stream` |
| `X-Sha256` | sha256 of the tar, 64 lowercase hex |
| `X-Artifact-Type` | `container-images` |
| `X-Artifact-Format` | `tar` |

Every file is complete on its own: files can arrive in any order and hours apart,
and the same file twice does no harm (import skips tags already at their digest).

Before the package is unpacked, NiFi sees only the outer FlowFile, so the first
route is on the filename. The second route, after unpacking, is on the attributes
mirror.py put inside.

## Before you start

- A directory on the high side for the inbox, for example `/data/container-images/inbox`,
  writable by the NiFi user and readable and writable by whoever runs `mirror.py import`.
- Where your diode flow hands over reassembled, decrypted files. This group takes
  them through an input port.
- Content repository room for about three times the largest file at once: the
  reassembled package, the unpacked tar and the repacked one.

## Part 1: parameter context

1. Top-right menu (**☰**) > **Parameter Contexts** > **+**.
2. **Settings** tab: Name `container-images-high`.
3. **Parameters** tab, add one parameter with **+**:

   | Name | Value | Sensitive |
   |---|---|---|
   | `import.inbox` | `/data/container-images/inbox` | No |

4. **Apply**.

## Part 2: process group

1. Drag the **Process Group** icon from the top toolbar onto the canvas. Name it
   `container-images-high` and click **Add**.
2. Right-click it > **Configure** > **Settings** tab: set **Parameter Context** to
   `container-images-high` and click **Apply**.
3. Double-click the group to go inside it. Parts 3 to 5 are built in here.

## Part 3: input port

Drag the **Input Port** icon onto the canvas. Name it `From diode` and click **Add**.

## Part 4: processors

For each one:
1. Drag the **Processor** icon onto the canvas, type the type into the filter, select
   it and click **Add**.
2. Right-click the processor > **Configure**.
3. **Settings** tab: set the Name.
4. **Properties** tab: set the listed properties. For a row marked *(add)*, click **+**
   at the top right of the properties table, enter the name, click **OK**, then enter
   the value.
5. **Relationships** tab: tick **terminate** for each relationship listed under
   Auto-terminate. Leave the others unticked, because the connections in Part 5 use
   them.
6. **Apply**.

Place them top to bottom in this order. Positions don't affect behaviour.

### 1. Container images only

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| Routing Strategy | `Route to Property name` (default) |
| `container-images` *(add)* | `${filename:matches('container-images-.*\.tar\.ffv3')}` |

Auto-terminate: `unmatched`. If the input port also takes other feeds, connect
`unmatched` to them instead.

### 2. Unpack FlowFile v3

Type **UnpackContent**.

| Property | Value |
|---|---|
| Packaging Format | `flowfile-stream-v3` |

Auto-terminate: `original`.

### 3. Attributes say container images

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| `container-images` *(add)* | `${X-Artifact-Type:equals('container-images'):and(${X-Artifact-Format:equals('tar')})}` |

Auto-terminate: none.

### 4. Hash tar

Type **CryptographicHashContent**.

| Property | Value |
|---|---|
| Hash Algorithm | `SHA-256` (default) |

Auto-terminate: none.

### 5. Tar matches X-Sha256

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| `verified` *(add)* | `${content_SHA-256:equals(${X-Sha256})}` |

Auto-terminate: none.

### 6. Package for import

Type **PackageFlowFile**, no properties.

Auto-terminate: `original`.

### 7. Name package

Type **UpdateAttribute**. PackageFlowFile names its output after the FlowFile UUID;
this puts the tar's name back.

| Property | Value |
|---|---|
| `filename` *(add)* | `${Filename}.ffv3` |

Auto-terminate: none.

### 8. Write import inbox

Type **PutFile**. PutFile writes a hidden temporary file and renames it once
complete, so import never reads half a file.

| Property | Value |
|---|---|
| Directory | `#{import.inbox}` |
| Conflict Resolution Strategy | `replace` |

Auto-terminate: `success`.

### 9. Rejected (inspect queue)

Type **UpdateAttribute**, no properties.

Auto-terminate: `success`. Keep it **stopped**, so anything that failed waits in its
input queue for you to inspect.

## Part 5: connections

For each row: hover over the source, drag the arrow that appears onto the
destination, tick exactly the listed relationship(s) on the **Details** tab, and click
**Add**. A connection from the input port has no relationship to tick.

| # | From | Relationship | To |
|---|---|---|---|
| 1 | From diode (port) | | Container images only |
| 2 | Container images only | `container-images` | Unpack FlowFile v3 |
| 3 | Unpack FlowFile v3 | `success` | Attributes say container images |
| 4 | Unpack FlowFile v3 | `failure` | Rejected (inspect queue) |
| 5 | Attributes say container images | `container-images` | Hash tar |
| 6 | Attributes say container images | `unmatched` | Rejected (inspect queue) |
| 7 | Hash tar | `success` | Tar matches X-Sha256 |
| 8 | Hash tar | `failure` | Rejected (inspect queue) |
| 9 | Tar matches X-Sha256 | `verified` | Package for import |
| 10 | Tar matches X-Sha256 | `unmatched` | Rejected (inspect queue) |
| 11 | Package for import | `success` | Name package |
| 12 | Name package | `success` | Write import inbox |
| 13 | Write import inbox | `failure` | Rejected (inspect queue) |

Then go back up to the parent canvas and connect your diode flow's output (the
reassembled, decrypted files) to the group's **From diode** port.

## Part 6: check and start

1. Every processor except **Rejected (inspect queue)** shows a stopped square, not a
   yellow warning triangle. Hover a triangle to read what is missing: usually an
   unterminated relationship or a mistyped property name.
2. Open each RouteOnAttribute > **Properties** and confirm that the only row with a
   **delete** icon is the one you added. A mistyped built-in property also shows up
   as a deletable row, even though the processor still reports valid.
3. Select everything except **Rejected (inspect queue)**, right-click > **Start**.
   Start the **From diode** port too.
4. Send a file from the low side. Pipeline: run it with `MIRROR_SEND_FLOWFILE=true`.
   By hand:

   ```sh
   python3 mirror.py export team/app:1.0 -o container-images-test.tar
   python3 mirror.py send container-images-test.tar --path /mnt/transfer --flowfile
   ```

5. Watch the queues: the package passes every step and
   `container-images-test.tar.ffv3` appears in the inbox. Nothing should reach
   **Rejected (inspect queue)**.
6. Import it on the high side:

   ```sh
   TARGET_REGISTRY=registry.high.example.internal python3 mirror.py import /data/container-images/inbox
   ```

   It prints `imported: <host>/team/app:1.0@sha256:...` and removes the file.
   Run it from cron or a systemd timer to finish the automation: a file that fails
   stays in the inbox and does not hold up the others.

## Push straight from NiFi

With the `nifi-container-registry-nar` installed (releases at github.com/djlongy/nifi-container-registry-processors), NiFi can push the
images itself, with no inbox, no skopeo and no `mirror.py import`: replace **Package for import**, **Name package**
and **Write import inbox** with one **PushContainerImages** fed by `verified` from **Tar matches X-Sha256**
(Registry URL, Username, Password, and an SSL Context Service for an internal CA). Connect its `failure` to
**Rejected (inspect queue)**: `registry.error` says why.

## Test without the diode

Inside the group, add **ListFile** (Input Directory a test directory, File Filter
`container-images-.*\.tar\.ffv3`) and **FetchFile** (Completion Strategy
`Delete File`; auto-terminate `not.found`, `permission.denied`, `failure`), and
connect ListFile `success` to FetchFile, and FetchFile `success` to **Container
images only**. Copy a `.ffv3` from the low side into the test directory. Remove both
once the diode feeds the port.

## When something reaches Rejected

Right-click the queue in front of **Rejected (inspect queue)** > **List queue** and
open the FlowFile's attributes:

- From **Unpack FlowFile v3**: the file is not a FlowFile v3 package, or it was cut
  short. Check the diode delivered it whole.
- From **Attributes say container images**: the name matched but `X-Artifact-Type`
  or `X-Artifact-Format` did not. Something other than mirror.py wrote it.
- From **Tar matches X-Sha256**: compare `content_SHA-256` with `X-Sha256`. The tar
  was damaged in transit or in reassembly. Send it again from the low side:
  `mirror.py export` the images by name, or run the pipeline with `EXPORT_ALL=true`.
- From **Write import inbox**: the inbox directory is missing or not writable by
  the NiFi user.
