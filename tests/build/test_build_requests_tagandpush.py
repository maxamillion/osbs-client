"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import copy
import json
import os
from pkg_resources import parse_version
import shutil

from osbs.build.build_request import BuildRequest
from osbs.constants import (DEFAULT_BUILD_IMAGE, DEFAULT_OUTER_TEMPLATE,
                            DEFAULT_INNER_TEMPLATE)
from osbs.exceptions import OsbsValidationException

from flexmock import flexmock
import pytest

from tests.constants import (INPUTS_PATH, TEST_BUILD_CONFIG, TEST_BUILD_JSON,
                             TEST_COMPONENT, TEST_GIT_BRANCH, TEST_GIT_REF,
                             TEST_GIT_URI, TEST_GIT_URI_HUMAN_NAME)


class NoSuchPluginException(Exception):
    pass


def get_sample_prod_params():
    return {
        'git_uri': TEST_GIT_URI,
        'git_ref': TEST_GIT_REF,
        'git_branch': TEST_GIT_BRANCH,
        'user': 'john-foo',
        'component': TEST_COMPONENT,
        'base_image': 'fedora:latest',
        'name_label': 'fedora/resultingimage',
        'registry_uri': 'registry.example.com',
        'source_registry_uri': 'registry.example.com',
        'openshift_uri': 'http://openshift/',
        'builder_openshift_url': 'http://openshift/',
        'koji_target': 'koji-target',
        'kojiroot': 'http://root/',
        'kojihub': 'http://hub/',
        'sources_command': 'make',
        'vendor': 'Foo Vendor',
        'authoritative_registry': 'registry.example.com',
        'distribution_scope': 'authoritative-source-only',
        'registry_api_versions': ['v2'],
        'pdc_url': 'https://pdc.example.com',
        'smtp_uri': 'smtp.example.com',
        'proxy': 'http://proxy.example.com'
    }


def get_plugins_from_build_json(build_json):
    env_vars = build_json['spec']['strategy']['customStrategy']['env']
    plugins = None

    for d in env_vars:
        if d['name'] == 'ATOMIC_REACTOR_PLUGINS':
            plugins = json.loads(d['value'])
            break

    assert plugins is not None
    return plugins


def get_plugin(plugins, plugin_type, plugin_name):
    plugins = plugins[plugin_type]
    for plugin in plugins:
        if plugin["name"] == plugin_name:
            return plugin
    else:
        raise NoSuchPluginException()


def has_plugin(plugins, plugin_type, plugin_name):
    try:
        get_plugin(plugins, plugin_type, plugin_name)
    except NoSuchPluginException:
        return False
    return True


def plugin_value_get(plugins, plugin_type, plugin_name, *args):
    result = get_plugin(plugins, plugin_type, plugin_name)
    for arg in args:
        result = result[arg]
    return result


def get_secret_mountpath_by_name(build_json, name):
    secrets = build_json['spec']['strategy']['customStrategy']['secrets']
    named_secrets = [secret for secret in secrets
                     if secret['secretSource']['name'] == name]
    assert len(named_secrets) == 1
    secret = named_secrets[0]
    assert 'mountPath' in secret
    return secret['mountPath']


class TestBuildRequest(object):
    def test_render_prod_custom_site_plugin_override_tag_and_push(self):
        """
        Test to make sure that when we attempt to override the tag_and_push
        plugin's args, they are actually overridden in the JSON for the
        build_request after running build_request.render() and the plugins
        are in the correct sequential order
        """



        plugin_type = "postbuild_plugins"
        plugin_name = "tag_and_push"
        #
        # plugin_args is different than below, the render() removes
        # "version": "v2"
        #
        plugin_args = {
            "registries": {
                "candidate-registry.stg.fedoraproject.org": {
                    "insecure": False
                }
            }
        }

        kwargs = get_sample_prod_params()

        unmodified_build_request = BuildRequest(INPUTS_PATH)
        unmodified_build_request.set_params(**kwargs)
        unmodified_build_request.render()

        for plugin_dict in unmodified_build_request.dj.dock_json[plugin_type]:
            if plugin_dict['name'] == plugin_name:
                plugin_index = unmodified_build_request.dj.dock_json[plugin_type].index(plugin_dict)

        build_request = BuildRequest(INPUTS_PATH)
        #build_request.customize_conf['enable_plugins'].append(
        #    {
        #        "plugin_type": plugin_type,
        #        "plugin_name": plugin_name,
        #        "plugin_args": plugin_args
        #    }
        #)
        build_request.customize_conf['enable_plugins'] = [
            {
                "plugin_type": "postbuild_plugins",
                "plugin_name": "tag_and_push",
                "plugin_args": {
                    "registries": {
                        "candidate-registry.stg.fedoraproject.org": {
                            "insecure": False,
                            "version": "v2"
                        }
                    }
                }
            },
            {
                "plugin_type": "prebuild_plugins",
                "plugin_name": "pull_base_image",
                "plugin_args": {
                    "parent_registry_insecure": False,
                    "parent_registry": "registry.stg.fedoraproject.org"
                }
            },
            {
                "plugin_type": "exit_plugins",
                "plugin_name": "koji_promote",
                "plugin_args": {
                    "kojihub": "https://koji.stg.fedoraproject.org/kojihub",
                    "verify_ssl": True,
                    "target": "rawhide-docker-candidate",
                    "url": "https://osbs.stg.fedoraproject.org/",
                    "blocksize": 10485760,
                    "koji_principal": "osbs/osbs.stg.fedoraproject.org@STG.FEDORAPROJECT.ORG",
                    "koji_keytab": "FILE:/etc/krb5.osbs_osbs.stg.fedoraproject.org.keytab",
                    "use_auth": True
                }
            },
            {
                "plugin_type": "exit_plugins",
                "plugin_name": "store_metadata_in_osv3",
                "plugin_args": {
                    "url": "https://osbs.stg.fedoraproject.org/",
                    "verify_ssl": False,
                    "use_auth": True
                }
            }
        ]
        build_request.customize_conf['disable_plugins'] = [
            {
                "plugin_type": "postbuild_plugins",
                "plugin_name": "pulp_push"
            },
            {
                "plugin_type": "postbuild_plugins",
                "plugin_name": "pulp_sync"
            },
            {
                "plugin_type": "postbuild_plugins",
                "plugin_name": "pulp_pull"
            }
        ]

        build_request.set_params(**kwargs)
        build_request.render()

        import pdb; pdb.set_trace()


        assert {
                "name": plugin_name,
                "args": plugin_args
        } in build_request.dj.dock_json[plugin_type]

        assert unmodified_build_request.dj.dock_json[plugin_type][plugin_index]['name'] == plugin_name
        assert build_request.dj.dock_json[plugin_type][plugin_index]['name'] == plugin_name
