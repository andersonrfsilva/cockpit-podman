# cockpit-podman

This is the [Cockpit](https://cockpit-project.org/) user interface for [podman
containers](https://podman.io/).

## Container domains

Expand a **system container** and select **Domains** to route HTTP traffic through
one shared Traefik proxy on host ports **80 and 443**. Enter a domain, the
container's HTTP port, and optional public/internal paths. Routes can be edited,
disabled, re-enabled, or deleted without recreating the application or
publishing its ports. HTTPS supports Let's Encrypt HTTP-01 or a PEM certificate
chain and unencrypted private key.

Routes of deleted or renamed containers appear in the Domains tab of any system
container as "removed or renamed" and can be deleted there. A new container with
the same name takes over the route again.

### Prerequisites: install and configure Traefik yourself

**cockpit-podman does not install Traefik or any packages, pull its image, create
a proxy or network, publish proxy ports, or start/restart a proxy service.**
The administrator must install and operate one existing Traefik 3 container in
system/rootful Podman before using Domains. This integration does not target a
native host binary or create a rootless proxy.

By default it uses container/network `cockpit-traefik` and routing directory
`/var/lib/cockpit-traefik`. To connect another existing installation, the
administrator can create `/etc/cockpit/traefik.json`:

```json
{
  "container": "traefik",
  "network": "proxy",
  "directory": "/var/lib/traefik"
}
```

This file only identifies existing resources; it does not provision them.
Before using the integration, the administrator must:

- Start Traefik 3 with host ports 80/443 and entrypoints named `web`/`websecure`.
- Prepare a DNS-enabled Podman bridge and attach the proxy to it.
- Use Podman 4.0 or newer: Domains requires it (netavark network format and
  `podman network exists`).
- Prepare a root-owned mode-0700 routing directory, outside unprivileged users'
  writable paths, with root-owned `dynamic/` and `certs/` subdirectories.
  Bind-mount these at `/etc/traefik/dynamic` and `/etc/traefik/certs` in Traefik,
  preferably read-only. The proxy must use the default root identity without
  user-namespace remapping so it can read mode-0600 routing files and keys.
- Enable Traefik's directory file provider with
  `providers.file.directory=/etc/traefik/dynamic` and `providers.file.watch=true`.
- For Let's Encrypt, configure a resolver named `letsencrypt`, its ACME contact
  email, account storage and HTTP-01 challenge outside Cockpit. Public TCP 80
  must reach this proxy. Cockpit never changes ACME settings or account storage.
- Configure DNS, firewall and boot persistence independently.

Missing prerequisites are displayed in Domains and saving stays disabled.
The fields remain available, and refreshing prerequisites preserves the draft.
Cockpit never offers to install or automatically start the missing proxy.

Only standalone system/rootful containers using bridge networking are supported.
Rootless containers cannot join the system network and do not get a separate
proxy. Pods and host/none/shared network namespaces are not supported.
Applications on the shared bridge can communicate with each other. Keep
container names stable; recreated containers must rejoin the configured bridge
with the same name or have their route saved again. Domains marks routes whose
container is missing, stopped or off the bridge, and offers Reconnect for the
last case. Removing a route leaves the network attachment in place.

The proxy uses Traefik's file provider, not an engine socket or container labels.
Cockpit writes only `dynamic/cockpit-podman.yml`, its own
`cockpit-podman-state.json`/lock, and keys under `certs/cockpit-podman/` in the
routing directory. An existing unowned file at the reserved configuration path
is refused. Operator configuration, other routes, certificates and ACME storage
are left alone. Avoid defining the same hostname/path both here and in other
Traefik configuration files; those external routes are not reconciled.

Updates are serialized and atomically published without restarting Traefik.
Private keys, route state and managed dynamic configuration are mode 0600;
the managed key subdirectory is mode 0700. Keys cross Cockpit's authenticated channel on stdin, not command
arguments. Neither application ports nor a Traefik dashboard are published by
this integration.

### Testing domain routing

`test/check-traefik` exercises route validation using the standard test runner.
`TestApplication.testDomainConfiguration` covers prerequisite failure, draft
preservation and certificate-mode controls without installing a proxy.

For a real smoke on a Linux development host:

```
sudo python3 test/verify-traefik
```

This opt-in check requires rootful Podman, Python and OpenSSL, with
`docker.io/library/traefik:v3.6.25` and `docker.io/library/python:3.13-alpine`
**already installed by the administrator**. `TRAEFIK_IMAGE` can select another
preinstalled Traefik 3 image. The check never downloads images: all fixture
containers use `--pull=never`.

The test harness provisions its own uniquely named fixtures and ephemeral
loopback-only ports to represent an external installation. The integration is
then exercised against that installation, without taking ownership of it.
Missing prerequisites must not create resources. Coverage includes shared-proxy
HTTP/path routing, prefix order, specificity, duplicate rejection, trusted PEM
TLS, redirects, route lifecycle, and preservation of the proxy's identity/start
time, operator routes, static configuration, ACME storage and private keys.
Stopping the fixture proxy must not trigger automatic startup. Fixtures are
removed in `finally`; the host proxy and preinstalled images are not modified.
Public ACME issuance and administrator-managed reboot persistence are not
exercised by this check.


## Technologies

 - cockpit-podman communicates to podman through its [REST API](https://docs.podman.io/en/latest/_static/api.html).
   Domain routing uses the Podman CLI through Cockpit's authenticated Python channel.

 - This project is based on the [Cockpit Starter Kit](https://github.com/cockpit-project/starter-kit).
   See [Starter Kit Intro](https://cockpit-project.org/blog/cockpit-starter-kit.html) for details.

# Development dependencies

On Debian/Ubuntu:

    $ sudo apt install gettext nodejs make

On Fedora:

    $ sudo dnf install gettext nodejs make

# Getting and building the source

These commands check out the source and build it into the `dist/` directory:

```
git clone https://github.com/cockpit-project/cockpit-podman
cd cockpit-podman
make
```

# Installing

`sudo make install` installs the package in `/usr/local/share/cockpit/`. This depends
on the `dist` target, which generates the distribution tarball.

You can also run `make rpm` to build RPMs for local installation.

In `production` mode, source files are automatically minified and compressed.
Set `NODE_ENV=production` if you want to duplicate this behavior.

# Development instructions

See [HACKING.md](./HACKING.md) for details about how to efficiently change the
code, run, and test it.

# Automated release

The intention is that the only manual step for releasing a project is to create
a signed tag for the version number, which includes a summary of the noteworthy
changes:

```
123

- this new feature
- fix bug #123
```

Pushing the release tag triggers the [release.yml](.github/workflows/release.yml)
[GitHub action](https://github.com/features/actions) workflow. This creates the
official release tarball and publishes as upstream release to GitHub.

The Fedora and COPR releases are done with [Packit](https://packit.dev/),
see the [packit.yaml](./packit.yaml) control file.

# Automated maintenance

It is important to keep your [NPM modules](./package.json) up to date, to keep
up with security updates and bug fixes. This happens with
[dependabot](https://github.com/dependabot),
see [configuration file](.github/dependabot.yml).

Translations are refreshed every Tuesday evening (or manually) through the
[weblate-sync-po.yml](.github/workflows/weblate-sync-po.yml) action.
Conversely, the PO template is uploaded to weblate every day through the
[weblate-sync-pot.yml](.github/workflows/weblate-sync-pot.yml) action.
