/* SPDX-License-Identifier: LGPL-2.1-or-later */
import React, { useEffect, useState } from 'react';

import { Alert } from '@patternfly/react-core/dist/esm/components/Alert';
import { Button } from '@patternfly/react-core/dist/esm/components/Button';
import { Checkbox } from '@patternfly/react-core/dist/esm/components/Checkbox';
import { Form, FormGroup } from '@patternfly/react-core/dist/esm/components/Form';
import { FormSelect, FormSelectOption } from '@patternfly/react-core/dist/esm/components/FormSelect';
import { TextArea } from '@patternfly/react-core/dist/esm/components/TextArea';
import { TextInput } from '@patternfly/react-core/dist/esm/components/TextInput';
import { Flex } from '@patternfly/react-core/dist/esm/layouts/Flex';
import { Stack, StackItem } from '@patternfly/react-core/dist/esm/layouts/Stack';

import cockpit from 'cockpit';
import * as python from 'python';

import helper from './traefik.py';

const _ = cockpit.gettext;
const emptyRoute = { host: '', port: '80', path: '/', internal_path: '/', tls: 'http', enabled: true, strip_path: false };

async function request(payload) {
    // Global proxy and routes always use the system store, never a user's Podman.
    const output = await python.spawn(helper, [], {
        superuser: 'require', err: 'message'
    }).input(JSON.stringify(payload));
    return JSON.parse(output);
}

export default function ContainerDomains({ container }) {
    const [state, setState] = useState(null);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [route, setRoute] = useState({ ...emptyRoute });
    const [editing, setEditing] = useState(null);
    const [certificate, setCertificate] = useState('');
    const [privateKey, setPrivateKey] = useState('');
    const [confirmDelete, setConfirmDelete] = useState(null);
    const supported = container.uid === 0;
    const name = container.Name.replace(/^\//, '');
    const prefix = `domains-${container.Id}`;
    const routes = Object.entries(state?.routes || {}).filter(([, item]) => item.container === name);
    const orphans = Object.entries(state?.routes || {}).filter(([, item]) => item.backend === 'missing' && item.container !== name);

    useEffect(() => {
        let active = true;
        setState(null);
        setError('');
        setRoute({ ...emptyRoute });
        setEditing(null);
        setCertificate('');
        setPrivateKey('');
        setConfirmDelete(null);
        if (supported) {
            request({ action: 'status' })
                    .then(value => {
                        if (active) {
                            setState(value);
                        }
                    })
                    .catch(ex => {
                        if (active)
                            setError(ex.message);
                    });
        }
        return () => { active = false };
    }, [supported, container.Id]);

    const perform = async payload => {
        setBusy(true);
        setError('');
        try {
            setState(await request(payload));
            if (payload.action !== 'status') {
                setRoute(null);
                setEditing(null);
                setCertificate('');
                setPrivateKey('');
                setConfirmDelete(null);
            }
        } catch (ex) {
            setError(ex.message);
        } finally {
            setBusy(false);
        }
    };

    const edit = (id, value) => {
        setEditing(id);
        setRoute({ ...value });
        setCertificate('');
        setPrivateKey('');
    };
    const field = (key, label, type = 'text') => (
        <FormGroup label={label} fieldId={`${prefix}-${key}`}>
            <TextInput id={`${prefix}-${key}`} type={type} isRequired value={route[key]}
                       min={type === 'number' ? 1 : undefined} max={type === 'number' ? 65535 : undefined}
                       onChange={(_event, value) => setRoute({ ...route, [key]: value })} isDisabled={busy} />
        </FormGroup>
    );

    // Also used for orphaned routes: the backend accepts them because their container no longer exists.
    const renderDelete = (id, item) => confirmDelete === id && (
        <Alert isInline variant="warning" title={cockpit.format(_("Delete domain $0?"), item.host + item.path)}>
            <Button variant="danger" isDisabled={busy || !state.ready} onClick={() => perform({ action: 'delete', id, container: name })}>{_("Delete route")}</Button>{' '}
            <Button variant="link" onClick={() => setConfirmDelete(null)}>{_("Cancel")}</Button>
        </Alert>
    );

    if (!supported)
        return (
            <Alert isInline variant="info" title={_("Domains use the single system Traefik on ports 80 and 443.")}>
                {_("Create this application as a system container to share the proxy network. User/rootless containers cannot join that network; no separate proxy will be created.")}
            </Alert>
        );

    if (state && name === state.proxy)
        return (
            <Alert isInline variant="info" title={_("Configure this Traefik instance outside Cockpit.")}>
                {_("Domain routing uses this existing proxy; Cockpit does not install or manage its lifecycle.")}
            </Alert>
        );

    return (
        <Stack hasGutter>
            {error && <StackItem><Alert isInline variant="danger" title={_("Traefik operation failed")}>{error}</Alert></StackItem>}
            {!state && !error && <StackItem>{_("Loading domains…")}</StackItem>}
            {state && <>
                <StackItem>
                    <Alert isInline variant={state.ready ? 'info' : 'warning'}
                           title={state.ready ? _("Using installed Traefik") : _("Traefik prerequisites not met")}>
                        {_("Traefik must be installed and configured by the administrator first. Cockpit only manages domain routes; it does not install packages, pull proxy images, create a proxy or network, publish ports, or start or restart Traefik.")}
                        {state.prerequisites.length > 0 && <ul>{state.prerequisites.map(item => <li key={item}>{item}</li>)}</ul>}
                        <Button variant="link" isInline isDisabled={busy} onClick={() => perform({ action: 'status' })}>{_("Refresh prerequisites")}</Button>
                    </Alert>
                </StackItem>
                <StackItem>
                    <Stack hasGutter>
                        {routes.map(([id, item]) => (
                            <StackItem key={id}>
                                <Flex gap={{ default: 'gapMd' }}>
                                    <span>{item.host}{item.path} → {item.container}:{item.port} · {item.tls === 'http' ? _("HTTP") : item.tls === 'letsencrypt' ? _("Let's Encrypt") : _("Custom certificate")} · {item.enabled ? _("Enabled") : _("Disabled")}</span>
                                    {item.backend === 'detached' && <>
                                        <span>{_("Not connected to the proxy network")}</span>
                                        <Button variant="link" isInline isDisabled={busy || !state.ready} onClick={() => perform({ action: 'save', id, container: container.Id, route: { ...item } })}>{_("Reconnect")}</Button>
                                    </>}
                                    {item.backend === 'stopped' && <span>{_("Container is not running")}</span>}
                                    {item.backend === 'missing' && <span>{_("Container not found")}</span>}
                                    <Button variant="link" isInline isDisabled={busy} aria-label={cockpit.format(_("Edit domain $0"), item.host + item.path)} onClick={() => edit(id, item)}>{_("Edit")}</Button>
                                    <Button variant="link" isInline isDisabled={busy || !state.ready} onClick={() => perform({ action: 'save', id, container: container.Id, route: { ...item, enabled: !item.enabled } })}>
                                        {item.enabled ? _("Disable") : _("Enable")}
                                    </Button>
                                    <Button variant="link" isInline isDanger isDisabled={busy || !state.ready} aria-label={cockpit.format(_("Delete domain $0"), item.host + item.path)} onClick={() => setConfirmDelete(id)}>{_("Delete")}</Button>
                                </Flex>
                                {renderDelete(id, item)}
                            </StackItem>
                        ))}
                        {!route && <StackItem><Button variant="secondary" isDisabled={busy} onClick={() => edit(null, emptyRoute)}>{_("Add domain")}</Button></StackItem>}
                    </Stack>
                </StackItem>
                {orphans.length > 0 && <StackItem>
                    <Alert isInline variant="warning" title={_("Routes for removed or renamed containers")}>
                        <Stack hasGutter>
                            {orphans.map(([id, item]) => (
                                <StackItem key={id}>
                                    <Flex gap={{ default: 'gapMd' }}>
                                        <span>{item.host}{item.path} → {item.container}:{item.port}</span>
                                        <Button variant="link" isInline isDanger isDisabled={busy || !state.ready} aria-label={cockpit.format(_("Delete domain $0"), item.host + item.path)} onClick={() => setConfirmDelete(id)}>{_("Delete")}</Button>
                                    </Flex>
                                    {renderDelete(id, item)}
                                </StackItem>
                            ))}
                        </Stack>
                    </Alert>
                </StackItem>}
            </>}
            {state && route && <StackItem>
                <Form onSubmit={event => {
                    event.preventDefault();
                    perform({ action: 'save', id: editing, container: container.Id, route, certificate, private_key: privateKey });
                }}>
                    {field('host', _("Domain name"))}
                    {field('port', _("Container HTTP port"), 'number')}
                    {field('path', _("Public path"))}
                    {field('internal_path', _("Internal path"))}
                    <Checkbox id={`${prefix}-strip`} label={_("Strip public path before adding internal path")}
                                  isChecked={route.strip_path} onChange={(_event, value) => setRoute({ ...route, strip_path: value })} isDisabled={busy} />
                    <FormGroup label={_("TLS")} fieldId={`${prefix}-tls`}>
                        <FormSelect id={`${prefix}-tls`} value={route.tls} isDisabled={busy} onChange={(_event, value) => setRoute({ ...route, tls: value })}>
                            <FormSelectOption value="http" label={_("HTTP only")} />
                            <FormSelectOption value="letsencrypt" label={_("HTTPS with Let’s Encrypt (HTTP-01)")} />
                            <FormSelectOption value="certificate" label={_("HTTPS with a PEM certificate")} />
                        </FormSelect>
                    </FormGroup>
                    {route.tls === 'certificate' && <>
                        <FormGroup label={_("PEM certificate chain (leave both blank to keep existing)")} fieldId={`${prefix}-cert`}>
                            <TextArea id={`${prefix}-cert`} value={certificate} spellCheck={false} autoComplete="off" onChange={(_event, value) => setCertificate(value)} isDisabled={busy} />
                        </FormGroup>
                        <FormGroup label={_("Unencrypted PEM private key")} fieldId={`${prefix}-key`}>
                            <TextArea id={`${prefix}-key`} value={privateKey} spellCheck={false} autoComplete="off" onChange={(_event, value) => setPrivateKey(value)} isDisabled={busy} />
                        </FormGroup>
                    </>}
                    {route.tls === 'letsencrypt' && <Alert isInline variant="info" title={_("Use the existing Let's Encrypt resolver")}>
                        {_("The administrator must configure the letsencrypt resolver, ACME contact email and HTTP-01 challenge in Traefik. Cockpit does not change static configuration or ACME storage. Public TCP port 80 must reach the existing proxy.")}
                    </Alert>}
                    <Checkbox id={`${prefix}-enabled`} label={_("Enable route")}
                                  isChecked={route.enabled} onChange={(_event, value) => setRoute({ ...route, enabled: value })} isDisabled={busy} />
                    <Alert isInline variant="info" title={_("Saving an enabled route connects this container to the existing proxy network.")}>
                        {cockpit.format(_("The administrator must prepare the shared $0 bridge first. No container restart or application host port publishing is needed. Recreated containers must rejoin that network with the same name. Pod, host-network and networkless containers are not supported."), state.network)}
                    </Alert>
                    <Flex>
                        <Button type="submit" isDisabled={busy || !state.ready} isLoading={busy}>
                            {_("Save domain")}
                        </Button>
                        <Button variant="link" isDisabled={busy} onClick={() => { setRoute(null); setCertificate(''); setPrivateKey('') }}>{_("Cancel")}</Button>
                    </Flex>
                </Form>
            </StackItem>}
            {error && !state && <StackItem><Button isDisabled={busy} onClick={() => perform({ action: 'status' })}>{_("Refresh")}</Button></StackItem>}
        </Stack>
    );
}
