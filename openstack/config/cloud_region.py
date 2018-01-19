# Copyright (c) 2014 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import math
import warnings

from keystoneauth1 import adapter
import keystoneauth1.exceptions.catalog
from keystoneauth1 import session as ks_session
import requestsexceptions
from six.moves import urllib

import openstack
from openstack import _log
from openstack.config import exceptions


def _make_key(key, service_type):
    if not service_type:
        return key
    else:
        service_type = service_type.lower().replace('-', '_')
        return "_".join([service_type, key])


def from_session(session, name=None, config=None, **kwargs):
    """Construct a CloudRegion from an existing `keystoneauth1.session.Session`

    When a Session already exists, we don't actually even need to go through
    the OpenStackConfig.get_one_cloud dance. We have a Session with Auth info.
    The only parameters that are really needed are adapter/catalog related.

    :param keystoneauth1.session.session session:
        An existing Session to use.
    :param str name:
        A name to use for this cloud region in logging. If left empty, the
        hostname of the auth_url found in the Session will be used.
    :param dict config:
        Config settings for this cloud region.
    """
    # If someone is constructing one of these from a Session, then they are
    # not using a named config. Use the hostname of their auth_url instead.
    name = name or urllib.parse.urlparse(session.auth.auth_url).hostname
    config = config or {}
    return CloudRegion(name=name, session=session, config=config, **kwargs)


class CloudRegion(object):
    """The configuration for a Region of an OpenStack Cloud.

    A CloudRegion encapsulates the config information needed for connections
    to all of the services in a Region of a Cloud.
    """
    def __init__(self, name, region_name=None, config=None,
                 force_ipv4=False, auth_plugin=None,
                 openstack_config=None, session_constructor=None,
                 app_name=None, app_version=None, session=None):
        self.name = name
        self.region_name = region_name
        self.config = config
        self.log = _log.setup_logging('openstack.config')
        self._force_ipv4 = force_ipv4
        self._auth = auth_plugin
        self._openstack_config = openstack_config
        self._keystone_session = session
        self._session_constructor = session_constructor or ks_session.Session
        self._app_name = app_name
        self._app_version = app_version

    def __getattr__(self, key):
        """Return arbitrary attributes."""

        if key.startswith('os_'):
            key = key[3:]

        if key in [attr.replace('-', '_') for attr in self.config]:
            return self.config[key]
        else:
            return None

    def __iter__(self):
        return self.config.__iter__()

    def __eq__(self, other):
        return (
            self.name == other.name
            and self.region_name == other.region_name
            and self.config == other.config)

    def __ne__(self, other):
        return not self == other

    def set_session_constructor(self, session_constructor):
        """Sets the Session constructor."""
        self._session_constructor = session_constructor

    def get_requests_verify_args(self):
        """Return the verify and cert values for the requests library."""
        if self.config['verify'] and self.config['cacert']:
            verify = self.config['cacert']
        else:
            verify = self.config['verify']
            if self.config['cacert']:
                warnings.warn(
                    "You are specifying a cacert for the cloud {0} but "
                    "also to ignore the host verification. The host SSL cert "
                    "will not be verified.".format(self.name))

        cert = self.config.get('cert', None)
        if cert:
            if self.config['key']:
                cert = (cert, self.config['key'])
        return (verify, cert)

    def get_services(self):
        """Return a list of service types we know something about."""
        services = []
        for key, val in self.config.items():
            if (key.endswith('api_version')
                    or key.endswith('service_type')
                    or key.endswith('service_name')):
                services.append("_".join(key.split('_')[:-2]))
        return list(set(services))

    def get_auth_args(self):
        return self.config.get('auth', {})

    def get_interface(self, service_type=None):
        key = _make_key('interface', service_type)
        interface = self.config.get('interface')
        return self.config.get(key, interface)

    def get_api_version(self, service_type):
        key = _make_key('api_version', service_type)
        return self.config.get(key, None)

    def get_service_type(self, service_type):
        key = _make_key('service_type', service_type)
        # Cinder did an evil thing where they defined a second service
        # type in the catalog. Of course, that's insane, so let's hide this
        # atrocity from the as-yet-unsullied eyes of our users.
        # Of course, if the user requests a volumev2, that structure should
        # still work.
        # What's even more amazing is that they did it AGAIN with cinder v3
        # And then I learned that mistral copied it.
        # TODO(shade) This should get removed when we have os-service-types
        # alias support landed in keystoneauth.
        if service_type in ('volume', 'block-storage'):
            vol_ver = self.get_api_version('volume')
            if vol_ver and vol_ver.startswith('2'):
                service_type = 'volumev2'
            elif vol_ver and vol_ver.startswith('3'):
                service_type = 'volumev3'
        elif service_type == 'workflow':
            wk_ver = self.get_api_version(service_type)
            if wk_ver and wk_ver.startswith('2'):
                service_type = 'workflowv2'
        return self.config.get(key, service_type)

    def get_service_name(self, service_type):
        key = _make_key('service_name', service_type)
        return self.config.get(key, None)

    def get_endpoint(self, service_type):
        key = _make_key('endpoint_override', service_type)
        old_key = _make_key('endpoint', service_type)
        return self.config.get(key, self.config.get(old_key, None))

    @property
    def prefer_ipv6(self):
        return not self._force_ipv4

    @property
    def force_ipv4(self):
        return self._force_ipv4

    def get_auth(self):
        """Return a keystoneauth plugin from the auth credentials."""
        return self._auth

    def get_session(self):
        """Return a keystoneauth session based on the auth credentials."""
        if self._keystone_session is None:
            if not self._auth:
                raise exceptions.OpenStackConfigException(
                    "Problem with auth parameters")
            (verify, cert) = self.get_requests_verify_args()
            # Turn off urllib3 warnings about insecure certs if we have
            # explicitly configured requests to tell it we do not want
            # cert verification
            if not verify:
                self.log.debug(
                    "Turning off SSL warnings for {cloud}:{region}"
                    " since verify=False".format(
                        cloud=self.name, region=self.region_name))
            requestsexceptions.squelch_warnings(insecure_requests=not verify)
            self._keystone_session = self._session_constructor(
                auth=self._auth,
                verify=verify,
                cert=cert,
                timeout=self.config['api_timeout'])
            if hasattr(self._keystone_session, 'additional_user_agent'):
                self._keystone_session.additional_user_agent.append(
                    ('openstacksdk', openstack.__version__))
            # Using old keystoneauth with new os-client-config fails if
            # we pass in app_name and app_version. Those are not essential,
            # nor a reason to bump our minimum, so just test for the session
            # having the attribute post creation and set them then.
            if hasattr(self._keystone_session, 'app_name'):
                self._keystone_session.app_name = self._app_name
            if hasattr(self._keystone_session, 'app_version'):
                self._keystone_session.app_version = self._app_version
        return self._keystone_session

    def get_service_catalog(self):
        """Helper method to grab the service catalog."""
        return self._auth.get_access(self.get_session()).service_catalog

    def _get_version_args(self, service_key, version):
        """Translate OCC version args to those needed by ksa adapter.

        If no version is requested explicitly and we have a configured version,
        set the version parameter and let ksa deal with expanding that to
        min=ver.0, max=ver.latest.

        If version is set, pass it through.

        If version is not set and we don't have a configured version, default
        to latest.
        """
        if version == 'latest':
            return None, None, 'latest'
        if not version:
            version = self.get_api_version(service_key)
        if not version:
            return None, None, 'latest'
        return version, None, None

    def get_session_client(self, service_key, version=None):
        """Return a prepped requests adapter for a given service.

        This is useful for making direct requests calls against a
        'mounted' endpoint. That is, if you do:

          client = get_session_client('compute')

        then you can do:

          client.get('/flavors')

        and it will work like you think.
        """
        (version, min_version, max_version) = self._get_version_args(
            service_key, version)

        return adapter.Adapter(
            session=self.get_session(),
            service_type=self.get_service_type(service_key),
            service_name=self.get_service_name(service_key),
            interface=self.get_interface(service_key),
            region_name=self.region_name,
            version=version,
            min_version=min_version,
            max_version=max_version)

    def _get_highest_endpoint(self, service_types, kwargs):
        session = self.get_session()
        for service_type in service_types:
            kwargs['service_type'] = service_type
            try:
                # Return the highest version we find that matches
                # the request
                return session.get_endpoint(**kwargs)
            except keystoneauth1.exceptions.catalog.EndpointNotFound:
                pass

    def get_session_endpoint(
            self, service_key, min_version=None, max_version=None):
        """Return the endpoint from config or the catalog.

        If a configuration lists an explicit endpoint for a service,
        return that. Otherwise, fetch the service catalog from the
        keystone session and return the appropriate endpoint.

        :param service_key: Generic key for service, such as 'compute' or
                            'network'

        """

        override_endpoint = self.get_endpoint(service_key)
        if override_endpoint:
            return override_endpoint
        endpoint = None
        kwargs = {
            'service_name': self.get_service_name(service_key),
            'region_name': self.region_name
        }
        kwargs['interface'] = self.get_interface(service_key)
        if service_key == 'volume' and not self.get_api_version('volume'):
            # If we don't have a configured cinder version, we can't know
            # to request a different service_type
            min_version = float(min_version or 1)
            max_version = float(max_version or 3)
            min_major = math.trunc(float(min_version))
            max_major = math.trunc(float(max_version))
            versions = range(int(max_major) + 1, int(min_major), -1)
            service_types = []
            for version in versions:
                if version == 1:
                    service_types.append('volume')
                else:
                    service_types.append('volumev{v}'.format(v=version))
        else:
            service_types = [self.get_service_type(service_key)]
        endpoint = self._get_highest_endpoint(service_types, kwargs)
        if not endpoint:
            self.log.warning(
                "Keystone catalog entry not found ("
                "service_type=%s,service_name=%s"
                "interface=%s,region_name=%s)",
                service_key,
                kwargs['service_name'],
                kwargs['interface'],
                kwargs['region_name'])
        return endpoint

    def get_cache_expiration_time(self):
        if self._openstack_config:
            return self._openstack_config.get_cache_expiration_time()

    def get_cache_path(self):
        if self._openstack_config:
            return self._openstack_config.get_cache_path()

    def get_cache_class(self):
        if self._openstack_config:
            return self._openstack_config.get_cache_class()

    def get_cache_arguments(self):
        if self._openstack_config:
            return self._openstack_config.get_cache_arguments()

    def get_cache_expiration(self):
        if self._openstack_config:
            return self._openstack_config.get_cache_expiration()

    def get_cache_resource_expiration(self, resource, default=None):
        """Get expiration time for a resource

        :param resource: Name of the resource type
        :param default: Default value to return if not found (optional,
                        defaults to None)

        :returns: Expiration time for the resource type as float or default
        """
        if self._openstack_config:
            expiration = self._openstack_config.get_cache_expiration()
            if resource not in expiration:
                return default
            return float(expiration[resource])

    def requires_floating_ip(self):
        """Return whether or not this cloud requires floating ips.


        :returns: True of False if know, None if discovery is needed.
                  If requires_floating_ip is not configured but the cloud is
                  known to not provide floating ips, will return False.
        """
        if self.config['floating_ip_source'] == "None":
            return False
        return self.config.get('requires_floating_ip')

    def get_external_networks(self):
        """Get list of network names for external networks."""
        return [
            net['name'] for net in self.config['networks']
            if net['routes_externally']]

    def get_external_ipv4_networks(self):
        """Get list of network names for external IPv4 networks."""
        return [
            net['name'] for net in self.config['networks']
            if net['routes_ipv4_externally']]

    def get_external_ipv6_networks(self):
        """Get list of network names for external IPv6 networks."""
        return [
            net['name'] for net in self.config['networks']
            if net['routes_ipv6_externally']]

    def get_internal_networks(self):
        """Get list of network names for internal networks."""
        return [
            net['name'] for net in self.config['networks']
            if not net['routes_externally']]

    def get_internal_ipv4_networks(self):
        """Get list of network names for internal IPv4 networks."""
        return [
            net['name'] for net in self.config['networks']
            if not net['routes_ipv4_externally']]

    def get_internal_ipv6_networks(self):
        """Get list of network names for internal IPv6 networks."""
        return [
            net['name'] for net in self.config['networks']
            if not net['routes_ipv6_externally']]

    def get_default_network(self):
        """Get network used for default interactions."""
        for net in self.config['networks']:
            if net['default_interface']:
                return net['name']
        return None

    def get_nat_destination(self):
        """Get network used for NAT destination."""
        for net in self.config['networks']:
            if net['nat_destination']:
                return net['name']
        return None

    def get_nat_source(self):
        """Get network used for NAT source."""
        for net in self.config['networks']:
            if net.get('nat_source'):
                return net['name']
        return None