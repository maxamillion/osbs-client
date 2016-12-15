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
        'registry_api_versions': ['v1'],
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
    def test_build_request_is_auto_instantiated(self):
        build_json = copy.deepcopy(TEST_BUILD_JSON)
        br = BuildRequest('something')
        flexmock(br).should_receive('template').and_return(build_json)
        assert br.is_auto_instantiated() is True

    def test_build_request_isnt_auto_instantiated(self):
        build_json = copy.deepcopy(TEST_BUILD_JSON)
        build_json['spec']['triggers'] = []
        br = BuildRequest('something')
        flexmock(br).should_receive('template').and_return(build_json)
        assert br.is_auto_instantiated() is False

    def test_set_label(self):
        build_json = copy.deepcopy(TEST_BUILD_JSON)
        br = BuildRequest('something')
        flexmock(br).should_receive('template').and_return(build_json)
        assert br.template['metadata'].get('labels') is None

        br.set_label('label-1', 'value-1')
        br.set_label('label-2', 'value-2')
        br.set_label('label-3', 'value-3')
        assert br.template['metadata']['labels'] == {
            'label-1': 'value-1',
            'label-2': 'value-2',
            'label-3': 'value-3',
        }

    @pytest.mark.parametrize('registry_uris', [
        [],
        ["registry.example.com:5000"],
        ["registry.example.com:5000", "localhost:6000"],
    ])
    @pytest.mark.parametrize('build_image', [
        None,
        'fancy_buildroot:latestest'
    ])
    def test_render_simple_request(self, registry_uris, build_image):
        build_request = BuildRequest(INPUTS_PATH)
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'registry_uris': registry_uris,
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'build_image': build_image,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'registry_api_versions': ['v1'],
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_json["metadata"]["name"] is not None
        assert "triggers" not in build_json["spec"]
        assert build_json["spec"]["source"]["git"]["uri"] == TEST_GIT_URI
        assert build_json["spec"]["source"]["git"]["ref"] == TEST_GIT_REF

        expected_output = "john-foo/component:none-20"
        if registry_uris:
            expected_output = registry_uris[0] + "/" + expected_output
        assert build_json["spec"]["output"]["to"]["name"].startswith(expected_output)

        plugins = get_plugins_from_build_json(build_json)
        pull_base_image = get_plugin(plugins, "prebuild_plugins",
                                     "pull_base_image")
        assert pull_base_image is not None
        assert ('args' not in pull_base_image or
                'parent_registry' not in pull_base_image['args'] or
                pull_base_image['args']['parent_registry'] == None)

        assert plugin_value_get(plugins, "exit_plugins", "store_metadata_in_osv3", "args", "url") == \
            "http://openshift/"

        for r in registry_uris:
            assert plugin_value_get(plugins, "postbuild_plugins", "tag_and_push", "args",
                                    "registries", r) == {"insecure": True}

        rendered_build_image = build_json["spec"]["strategy"]["customStrategy"]["from"]["name"]
        assert rendered_build_image == (build_image if build_image else DEFAULT_BUILD_IMAGE)

    @pytest.mark.parametrize('proxy', [
        None,
        'http://proxy.example.com',
    ])
    @pytest.mark.parametrize('build_image', [
        None,
        'ultimate-buildroot:v1.0'
    ])
    @pytest.mark.parametrize('build_imagestream', [
        None,
        'buildroot-stream:v1.0'
    ])
    def test_render_prod_request_with_repo(self, build_image, build_imagestream, proxy):
        build_request = BuildRequest(INPUTS_PATH)
        name_label = "fedora/resultingimage"
        vendor = "Foo Vendor"
        authoritative_registry = "registry.example.com"
        distribution_scope = "authoritative-source-only"
        koji_task_id = 4756
        assert isinstance(build_request, BuildRequest)
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uri': "registry.example.com",
            'source_registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'koji_task_id': koji_task_id,
            'sources_command': "make",
            'vendor': vendor,
            'authoritative_registry': authoritative_registry,
            'distribution_scope': distribution_scope,
            'yum_repourls': ["http://example.com/my.repo"],
            'registry_api_versions': ['v1'],
            'build_image': build_image,
            'build_imagestream': build_imagestream,
            'proxy': proxy,
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_json["metadata"]["name"] == TEST_BUILD_CONFIG
        assert build_json["metadata"]["labels"]["koji-task-id"] == str(koji_task_id)
        assert "triggers" not in build_json["spec"]
        assert build_json["spec"]["source"]["git"]["uri"] == TEST_GIT_URI
        assert build_json["spec"]["source"]["git"]["ref"] == TEST_GIT_REF
        assert build_json["spec"]["output"]["to"]["name"].startswith(
            "registry.example.com/john-foo/component:"
        )

        plugins = get_plugins_from_build_json(build_json)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins",
                       "stop_autorebuild_if_disabled")

        assert plugin_value_get(plugins, "prebuild_plugins", "bump_release",
                                "args", "hub") == "http://hub/"

        assert plugin_value_get(plugins, "prebuild_plugins", "distgit_fetch_artefacts",
                                "args", "command") == "make"
        assert plugin_value_get(plugins, "prebuild_plugins", "pull_base_image",
                                "args", "parent_registry") == "registry.example.com"
        assert plugin_value_get(plugins, "exit_plugins", "store_metadata_in_osv3",
                                "args", "url") == "http://openshift/"
        assert plugin_value_get(plugins, "postbuild_plugins", "tag_and_push", "args",
                                "registries", "registry.example.com") == {"insecure": True}
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "koji")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "cp_built_image_to_nfs")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_push")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_sync")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "sendmail")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, 'exit_plugins', 'delete_from_registry')

        assert plugin_value_get(plugins, "prebuild_plugins", "add_yum_repo_by_url",
                                "args", "repourls") == ["http://example.com/my.repo"]
        if proxy:
            assert plugin_value_get(plugins, "prebuild_plugins", "add_yum_repo_by_url",
                                    "args", "inject_proxy") == proxy
        else:
            with pytest.raises(KeyError):
                plugin_value_get(plugins, "prebuild_plugins", "add_yum_repo_by_url",
                                 "args", "inject_proxy")

        labels = plugin_value_get(plugins, "prebuild_plugins", "add_labels_in_dockerfile",
                                  "args", "labels")

        assert labels is not None
        assert labels['authoritative-source-url'] == authoritative_registry
        assert labels['vendor'] == vendor
        assert labels['distribution-scope'] == distribution_scope

        rendered_build_image = build_json["spec"]["strategy"]["customStrategy"]["from"]["name"]
        if not build_imagestream:
            assert rendered_build_image == (build_image if build_image else DEFAULT_BUILD_IMAGE)
        else:
            assert rendered_build_image == build_imagestream
            assert build_json["spec"]["strategy"]["customStrategy"]["from"]["kind"] == "ImageStreamTag"

    @pytest.mark.parametrize('proxy', [
        None,
        'http://proxy.example.com',
    ])
    def test_render_prod_request(self, proxy):
        build_request = BuildRequest(INPUTS_PATH)
        name_label = "fedora/resultingimage"
        koji_target = "koji-target"
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uri': "registry.example.com",
            'source_registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': koji_target,
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
            'pdc_url': 'https://pdc.example.com',
            'smtp_uri': 'smtp.example.com',
            'proxy': proxy
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_json["metadata"]["name"] == TEST_BUILD_CONFIG
        assert "triggers" not in build_json["spec"]
        assert build_json["spec"]["source"]["git"]["uri"] == TEST_GIT_URI
        assert build_json["spec"]["source"]["git"]["ref"] == TEST_GIT_REF
        assert build_json["spec"]["output"]["to"]["name"].startswith(
            "registry.example.com/john-foo/component:"
        )
        assert build_json["metadata"]["labels"]["git-repo-name"] == TEST_GIT_URI_HUMAN_NAME
        assert build_json["metadata"]["labels"]["git-branch"] == TEST_GIT_BRANCH

        plugins = get_plugins_from_build_json(build_json)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins",
                       "stop_autorebuild_if_disabled")

        assert plugin_value_get(plugins, "prebuild_plugins", "bump_release",
                                "args", "hub") == "http://hub/"

        assert plugin_value_get(plugins, "prebuild_plugins", "distgit_fetch_artefacts",
                                "args", "command") == "make"
        assert plugin_value_get(plugins, "prebuild_plugins", "pull_base_image", "args",
                                "parent_registry") == "registry.example.com"
        assert plugin_value_get(plugins, "exit_plugins", "store_metadata_in_osv3",
                                "args", "url") == "http://openshift/"
        assert plugin_value_get(plugins, "prebuild_plugins", "koji",
                                "args", "root") == "http://root/"
        assert plugin_value_get(plugins, "prebuild_plugins", "koji",
                                "args", "target") == koji_target
        assert plugin_value_get(plugins, "prebuild_plugins", "koji",
                                "args", "hub") == "http://hub/"
        if proxy:
            assert plugin_value_get(plugins, "prebuild_plugins", "koji",
                                    "args", "proxy") == proxy
        else:
            with pytest.raises(KeyError):
                plugin_value_get(plugins, "prebuild_plugins", "koji", "args", "proxy")

        assert plugin_value_get(plugins, "postbuild_plugins", "tag_and_push", "args",
                                "registries", "registry.example.com") == {"insecure": True}
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "cp_built_image_to_nfs")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_push")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_sync")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "sendmail")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, 'exit_plugins', 'delete_from_registry')
        assert get_plugin(plugins, "exit_plugins", "koji_promote")
        assert plugin_value_get(plugins, "exit_plugins", "koji_promote", "args",
                                "target") == koji_target

        labels = plugin_value_get(plugins, "prebuild_plugins", "add_labels_in_dockerfile",
                                  "args", "labels")

        assert labels is not None
        assert labels['authoritative-source-url'] is not None
        assert labels['vendor'] is not None
        assert labels['distribution-scope'] is not None

    def test_render_prod_without_koji_request(self):
        build_request = BuildRequest(INPUTS_PATH)
        name_label = "fedora/resultingimage"
        assert isinstance(build_request, BuildRequest)
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uri': "registry.example.com",
            'source_registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_json["metadata"]["name"] == TEST_BUILD_CONFIG
        assert "triggers" not in build_json["spec"]
        assert build_json["spec"]["source"]["git"]["uri"] == TEST_GIT_URI
        assert build_json["spec"]["source"]["git"]["ref"] == TEST_GIT_REF
        assert build_json["spec"]["output"]["to"]["name"].startswith(
            "registry.example.com/john-foo/component:none-"
        )

        plugins = get_plugins_from_build_json(build_json)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins",
                       "stop_autorebuild_if_disabled")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "bump_release")
        assert plugin_value_get(plugins, "prebuild_plugins", "distgit_fetch_artefacts",
                                "args", "command") == "make"
        assert plugin_value_get(plugins, "prebuild_plugins", "pull_base_image", "args",
                                "parent_registry") == "registry.example.com"
        assert plugin_value_get(plugins, "exit_plugins", "store_metadata_in_osv3",
                                "args", "url") == "http://openshift/"
        assert plugin_value_get(plugins, "postbuild_plugins", "tag_and_push", "args",
                                "registries", "registry.example.com") == {"insecure": True}

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "koji")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "cp_built_image_to_nfs")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_push")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_sync")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "koji_promote")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "sendmail")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, 'exit_plugins', 'delete_from_registry')

        labels = plugin_value_get(plugins, "prebuild_plugins", "add_labels_in_dockerfile",
                                  "args", "labels")

        assert labels is not None
        assert labels['authoritative-source-url'] is not None
        assert labels['vendor'] is not None
        assert labels['distribution-scope'] is not None

    def test_render_prod_with_secret_request(self):
        build_request = BuildRequest(INPUTS_PATH)
        assert isinstance(build_request, BuildRequest)
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'registry_uri': "",
            'pulp_registry': "registry.example.com",
            'nfs_server_path': "server:path",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
            'source_secret': 'mysecret',
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        # Check that the secret's mountPath matches the plugin's
        # configured path for the secret
        mount_path = get_secret_mountpath_by_name(build_json, 'mysecret')
        plugins = get_plugins_from_build_json(build_json)
        assert get_plugin(plugins, "postbuild_plugins", "pulp_push")
        assert plugin_value_get(plugins, 'postbuild_plugins', 'pulp_push',
                                'args', 'pulp_secret_path') == mount_path

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins",
                       "stop_autorebuild_if_disabled")

        assert plugin_value_get(plugins, "prebuild_plugins", "bump_release",
                                "args", "hub") == "http://hub/"

        assert get_plugin(plugins, "prebuild_plugins", "koji")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_sync")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "cp_built_image_to_nfs")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "sendmail")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, 'exit_plugins', 'delete_from_registry')
        assert plugin_value_get(plugins, "postbuild_plugins", "tag_and_push", "args",
                                "registries") == {}

    @pytest.mark.parametrize('registry_secrets', [None, ['registry-secret']])
    @pytest.mark.parametrize('source_registry', [None, 'registry.example.com', 'localhost'])
    def test_render_pulp_sync(self, registry_secrets, source_registry):
        build_request = BuildRequest(INPUTS_PATH)
        pulp_env = 'env'
        pulp_secret = 'pulp-secret'
        registry_uri = 'https://registry.example.com'
        registry_ver = '/v2'
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'registry_uri': registry_uri + registry_ver,
            'openshift_uri': "http://openshift/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v2'],
            'registry_secrets': registry_secrets,
            'pulp_registry': pulp_env,
            'pulp_secret': pulp_secret,
        }
        if source_registry:
            kwargs['source_registry_uri'] = source_registry

        build_request.set_params(**kwargs)
        build_json = build_request.render()
        plugins = get_plugins_from_build_json(build_json)

        assert get_plugin(plugins, 'postbuild_plugins', 'pulp_sync')
        assert plugin_value_get(plugins, 'postbuild_plugins',
                                'pulp_sync', 'args',
                                'pulp_registry_name') == pulp_env
        assert plugin_value_get(plugins, 'postbuild_plugins',
                                'pulp_sync', 'args',
                                'docker_registry') == registry_uri

        if source_registry and source_registry in kwargs['registry_uri']:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'exit_plugins', 'delete_from_registry')
        else:
            assert get_plugin(plugins, 'exit_plugins', 'delete_from_registry')
            assert 'https://registry.example.com' in plugin_value_get(plugins, 'exit_plugins',
                                                                      'delete_from_registry',
                                                                      'args', 'registries')

            if registry_secrets:
                assert plugin_value_get(plugins, 'exit_plugins',
                                        'delete_from_registry', 'args',
                                        'registries', 'https://registry.example.com', 'secret')
            else:
                assert plugin_value_get(plugins, 'exit_plugins',
                                        'delete_from_registry', 'args',
                                        'registries', 'https://registry.example.com') == {}


        if registry_secrets:
            mount_path = get_secret_mountpath_by_name(build_json,
                                                      registry_secrets[0])
            assert plugin_value_get(plugins, 'postbuild_plugins',
                                    'pulp_sync', 'args',
                                    'registry_secret_path') == mount_path

        mount_path = get_secret_mountpath_by_name(build_json, pulp_secret)
        assert plugin_value_get(plugins, 'postbuild_plugins',
                                'pulp_sync', 'args',
                                'pulp_secret_path') == mount_path

    def test_render_prod_with_registry_secrets(self):
        build_request = BuildRequest(INPUTS_PATH)

        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'registry_uri': "registry.example.com",
            'nfs_server_path': "server:path",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
            'source_secret': 'mysecret',
            'registry_secrets': ['registry_secret'],
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        mount_path = get_secret_mountpath_by_name(build_json, 'registry_secret')
        plugins = get_plugins_from_build_json(build_json)
        assert get_plugin(plugins, "postbuild_plugins", "tag_and_push")
        assert plugin_value_get(
            plugins, "postbuild_plugins", "tag_and_push", "args", "registries",
            "registry.example.com", "secret") == mount_path

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins",
                       "stop_autorebuild_if_disabled")
        assert get_plugin(plugins, "prebuild_plugins", "bump_release")
        assert get_plugin(plugins, "prebuild_plugins", "koji")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_sync")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "cp_built_image_to_nfs")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "sendmail")

    def test_render_prod_request_requires_newer(self):
        """
        We should get an OsbsValidationException when trying to use the
        sendmail plugin without requiring OpenShift 1.0.6, as
        configuring the plugin requires the new-style secrets.
        """
        build_request = BuildRequest(INPUTS_PATH)
        name_label = "fedora/resultingimage"
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uris': ["registry1.example.com/v1",  # first is primary
                              "registry2.example.com/v2"],
            'nfs_server_path': "server:path",
            'source_registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'pdc_secret': 'foo',
            'pdc_url': 'https://pdc.example.com',
            'smtp_uri': 'smtp.example.com',
        }
        build_request.set_params(**kwargs)
        with pytest.raises(OsbsValidationException):
            build_request.render()

    @pytest.mark.parametrize('registry_api_versions', [
        ['v1', 'v2'],
        ['v2'],
    ])
    @pytest.mark.parametrize('scratch', [False, True])
    def test_render_prod_request_v1_v2(self, registry_api_versions, scratch):
        build_request = BuildRequest(INPUTS_PATH)
        name_label = "fedora/resultingimage"
        pulp_env = 'v1pulp'
        pulp_secret = pulp_env + 'secret'
        registry_secret = 'registry_secret'
        kwargs = {
            'pulp_registry': pulp_env,
            'pulp_secret': pulp_secret,
        }

        kwargs.update({
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uris': [
                # first is primary
                "http://registry1.example.com:5000/v1",

                "http://registry2.example.com:5000/v2"
            ],
            'registry_secrets': [
                "",
                registry_secret,
            ],
            'nfs_server_path': "server:path",
            'source_registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': registry_api_versions,
            'scratch': scratch,
        })
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_json["metadata"]["name"] == TEST_BUILD_CONFIG
        assert "triggers" not in build_json["spec"]
        assert build_json["spec"]["source"]["git"]["uri"] == TEST_GIT_URI
        assert build_json["spec"]["source"]["git"]["ref"] == TEST_GIT_REF

        # Pulp used, so no direct registry output
        assert build_json["spec"]["output"]["to"]["name"].startswith(
            "john-foo/component:"
        )

        plugins = get_plugins_from_build_json(build_json)

        # tag_and_push configuration. Must not have the scheme part.
        expected_registries = {
            'registry2.example.com:5000': {
                'insecure': True,
                'secret': '/var/run/secrets/atomic-reactor/registry_secret'
            },
        }

        if 'v1' in registry_api_versions:
            expected_registries['registry1.example.com:5000'] = {
                'insecure': True,
            }

        assert plugin_value_get(plugins, "postbuild_plugins", "tag_and_push",
                                "args", "registries") == expected_registries

        secrets = build_json['spec']['strategy']['customStrategy']['secrets']
        for version, plugin in [('v1', 'pulp_push'), ('v2', 'pulp_sync')]:
            if version not in registry_api_versions:
                continue

            path = plugin_value_get(plugins, "postbuild_plugins", plugin,
                                        "args", "pulp_secret_path")
            mount_path = get_secret_mountpath_by_name(build_json, pulp_secret)
            assert mount_path == path

            if plugin == 'pulp_sync':
                path = plugin_value_get(plugins, "postbuild_plugins", plugin,
                                        "args", "registry_secret_path")
                mount_path = get_secret_mountpath_by_name(build_json,
                                                          registry_secret)
                assert mount_path == path

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "cp_built_image_to_nfs")

        if 'v1' in registry_api_versions:
            assert get_plugin(plugins, "postbuild_plugins",
                              "pulp_push")
            assert plugin_value_get(plugins, "postbuild_plugins", "pulp_push",
                                    "args", "pulp_registry_name") == pulp_env
        else:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, "postbuild_plugins",
                           "pulp_push")

        if 'v2' in registry_api_versions:
            assert get_plugin(plugins, "postbuild_plugins", "pulp_sync")
            env = plugin_value_get(plugins, "postbuild_plugins", "pulp_sync",
                                   "args", "pulp_registry_name")
            assert env == pulp_env

            pulp_secret = plugin_value_get(plugins, "postbuild_plugins",
                                           "pulp_sync", "args",
                                           "pulp_secret_path")
            docker_registry = plugin_value_get(plugins, "postbuild_plugins",
                                               "pulp_sync", "args",
                                               "docker_registry")

            # pulp_sync config must have the scheme part to satisfy pulp.
            assert docker_registry == 'http://registry2.example.com:5000'
        else:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, "postbuild_plugins", "pulp_sync")

        if scratch:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, "postbuild_plugins", "compress")

            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, "exit_plugins", "koji_promote")
        else:
            assert get_plugin(plugins, "postbuild_plugins", "compress")
            assert get_plugin(plugins, "exit_plugins", "koji_promote")

    def test_render_with_yum_repourls(self):
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
        }
        build_request = BuildRequest(INPUTS_PATH)

        # Test validation for yum_repourls parameter
        kwargs['yum_repourls'] = 'should be a list'
        with pytest.raises(OsbsValidationException):
            build_request.set_params(**kwargs)

        # Use a valid yum_repourls parameter and check the result
        kwargs['yum_repourls'] = ['http://example.com/repo1.repo', 'http://example.com/repo2.repo']
        build_request.set_params(**kwargs)
        build_json = build_request.render()
        strategy = build_json['spec']['strategy']['customStrategy']['env']
        plugins = get_plugins_from_build_json(build_json)

        repourls = None
        for d in plugins['prebuild_plugins']:
            if d['name'] == 'add_yum_repo_by_url':
                repourls = d['args']['repourls']

        assert repourls is not None
        assert len(repourls) == 2
        assert 'http://example.com/repo1.repo' in repourls
        assert 'http://example.com/repo2.repo' in repourls

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins",
                       "stop_autorebuild_if_disabled")

        assert plugin_value_get(plugins, "prebuild_plugins", "bump_release",
                                "args", "hub") == "http://hub/"

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "koji")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "cp_built_image_to_nfs")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_push")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "pulp_sync")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "sendmail")

    @pytest.mark.parametrize(('hub', 'disabled'), [
        ('http://hub/', False),
        (None, True),
    ])
    def test_render_bump_release(self, hub, disabled):
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
        }

        if hub:
            kwargs['kojihub'] = hub

        build_request = BuildRequest(INPUTS_PATH)
        build_request.set_params(**kwargs)
        build_json = build_request.render()
        strategy = build_json['spec']['strategy']['customStrategy']['env']
        plugins = get_plugins_from_build_json(build_json)

        if disabled:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, "prebuild_plugins", "bump_release")

        else:
            assert plugin_value_get(plugins, "prebuild_plugins", "bump_release",
                                    "args", "hub") == hub

    @pytest.mark.parametrize('unique_tag_only', [False, None, True])
    def test_render_unique_tag_only(self, unique_tag_only):
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'openshift_uri': "http://openshift/",
            'registry_api_versions': ['v1', 'v2'],
        }

        if unique_tag_only is not None:
            kwargs['unique_tag_only'] = unique_tag_only

        build_request = BuildRequest(INPUTS_PATH)
        build_request.set_params(**kwargs)
        build_json = build_request.render()
        strategy = build_json['spec']['strategy']['customStrategy']['env']
        plugins = get_plugins_from_build_json(build_json)

        if unique_tag_only:
            assert plugin_value_get(plugins,
                                    'postbuild_plugins',
                                    'tag_by_labels',
                                    'args',
                                    'unique_tag_only') == True
            assert not has_plugin(plugins, 'postbuild_plugins', 'tag_from_config')
        else:
            tag_and_push_args = get_plugin(plugins,
                                           'postbuild_plugins',
                                           'tag_and_push')['args']
            assert 'unique_tag_only' not in tag_and_push_args
            assert has_plugin(plugins, 'postbuild_plugins', 'tag_from_config')

    def test_render_prod_with_pulp_no_auth(self):
        """
        Rendering should fail if pulp is specified but auth config isn't
        """
        build_request = BuildRequest(INPUTS_PATH)
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': 'fedora/resultingimage',
            'registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'pulp_registry': "foo",
        }
        build_request.set_params(**kwargs)
        with pytest.raises(OsbsValidationException):
            build_request.render()

    @staticmethod
    def create_image_change_trigger_json(outdir):
        """
        Create JSON templates with an image change trigger added.

        :param outdir: str, path to store modified templates
        """

        # Make temporary copies of the JSON files
        for basename in [DEFAULT_OUTER_TEMPLATE, DEFAULT_INNER_TEMPLATE]:
            shutil.copy(os.path.join(INPUTS_PATH, basename),
                        os.path.join(outdir, basename))

        # Create a build JSON description with an image change trigger
        with open(os.path.join(outdir, DEFAULT_OUTER_TEMPLATE), 'r+') as prod_json:
            build_json = json.load(prod_json)

            # Add the image change trigger
            build_json['spec']['triggers'] = [
                {
                    "type": "ImageChange",
                    "imageChange": {
                        "from": {
                            "kind": "ImageStreamTag",
                            "name": "{{BASE_IMAGE_STREAM}}"
                        }
                    }
                }
            ]

            prod_json.seek(0)
            json.dump(build_json, prod_json)
            prod_json.truncate()

    @pytest.mark.parametrize(('registry_uri', 'insecure_registry'), [
        ("https://registry.example.com", False),
        ("http://registry.example.com", True),
    ])
    def test_render_prod_request_with_trigger(self, tmpdir, registry_uri,
                                              insecure_registry):
        self.create_image_change_trigger_json(str(tmpdir))
        build_request = BuildRequest(str(tmpdir))
        name_label = "fedora/resultingimage"
        pdc_secret_name = 'foo'
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uri': registry_uri,
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
            'pdc_secret': pdc_secret_name,
            'pdc_url': 'https://pdc.example.com',
            'smtp_uri': 'smtp.example.com',
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert "triggers" in build_json["spec"]
        assert build_json["spec"]["triggers"][0]["imageChange"]["from"]["name"] == 'fedora:latest'

        plugins = get_plugins_from_build_json(build_json)

        assert get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")
        assert get_plugin(plugins, "prebuild_plugins",
                          "stop_autorebuild_if_disabled")
        assert plugin_value_get(plugins, "prebuild_plugins",
                                "check_and_set_rebuild", "args",
                                "url") == kwargs["openshift_uri"]
        assert get_plugin(plugins, "postbuild_plugins", "import_image")
        assert plugin_value_get(plugins,
                                "postbuild_plugins", "import_image", "args",
                                "imagestream") == name_label.replace('/', '-')
        expected_repo = os.path.join(kwargs["registry_uri"], name_label)
        expected_repo = expected_repo.replace('https://', '')
        expected_repo = expected_repo.replace('http://', '')
        assert plugin_value_get(plugins,
                                "postbuild_plugins", "import_image", "args",
                                "docker_image_repo") == expected_repo
        assert plugin_value_get(plugins,
                                "postbuild_plugins", "import_image", "args",
                                "url") == kwargs["openshift_uri"]
        if insecure_registry:
            assert plugin_value_get(plugins,
                                    "postbuild_plugins", "import_image", "args",
                                    "insecure_registry")
        else:
            with pytest.raises(KeyError):
                plugin_value_get(plugins,
                                 "postbuild_plugins", "import_image", "args",
                                 "insecure_registry")

        assert plugin_value_get(plugins, "postbuild_plugins", "tag_and_push", "args",
                                "registries", "registry.example.com") == {"insecure": True}
        assert get_plugin(plugins, "exit_plugins", "koji_promote")
        assert plugin_value_get(plugins, "exit_plugins", "koji_promote",
                                "args", "kojihub") == kwargs["kojihub"]
        assert plugin_value_get(plugins, "exit_plugins", "koji_promote",
                                "args", "url") == kwargs["openshift_uri"]
        with pytest.raises(KeyError):
            plugin_value_get(plugins, 'exit_plugins', 'koji_promote',
                             'args', 'metadata_only')  # v1 enabled by default

        mount_path = get_secret_mountpath_by_name(build_json, pdc_secret_name)
        expected = {'args': {'from_address': 'osbs@example.com',
                             'url': 'http://openshift/',
                             'pdc_url': 'https://pdc.example.com',
                             'pdc_secret_path': mount_path,
                             'send_on': ['auto_fail', 'auto_success'],
                             'error_addresses': ['errors@example.com'],
                             'smtp_uri': 'smtp.example.com',
                             'submitter': 'john-foo'},
                    'name': 'sendmail'}
        assert get_plugin(plugins, 'exit_plugins', 'sendmail') == expected

    def test_render_custom_base_image_with_trigger(self, tmpdir):
        self.create_image_change_trigger_json(str(tmpdir))
        build_request = BuildRequest(str(tmpdir))

        kwargs = get_sample_prod_params()
        kwargs['base_image'] = 'koji/image-build'
        kwargs['yum_repourls'] = ["http://example.com/my.repo"]
        kwargs['pdc_secret'] = 'foo'
        kwargs['pdc_url'] = 'https://pdc.example.com'
        kwargs['smtp_uri'] = 'smtp.example.com'

        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_request.is_custom_base_image() is True

        # Verify the triggers are now disabled
        assert "triggers" not in build_json["spec"]

        # Verify the rebuild plugins are all disabled
        plugins = get_plugins_from_build_json(build_json)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "check_and_set_rebuild")

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins",
                       "stop_autorebuild_if_disabled")

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "sendmail")

    def test_render_prod_request_new_secrets(self, tmpdir):
        secret_name = 'mysecret'
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': "fedora/resultingimage",
            'registry_uri': "registry.example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'sources_command': "make",
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
            'pulp_registry': 'foo',
            'pulp_secret': secret_name,
        }

        # Default required version (1.0.6), implicitly and explicitly
        for required in (None, parse_version('1.0.6')):
            build_request = BuildRequest(INPUTS_PATH)
            if required is not None:
                build_request.set_openshift_required_version(required)

            build_request.set_params(**kwargs)
            build_json = build_request.render()

            # Not using the sourceSecret scheme
            assert 'sourceSecret' not in build_json['spec']['source']

            # Check that the secret's mountPath matches the plugin's
            # configured path for the secret
            mount_path = get_secret_mountpath_by_name(build_json, secret_name)
            plugins = get_plugins_from_build_json(build_json)
            assert plugin_value_get(plugins, 'postbuild_plugins', 'pulp_push',
                                    'args', 'pulp_secret_path') == mount_path

    def test_render_prod_request_with_koji_secret(self, tmpdir):
        self.create_image_change_trigger_json(str(tmpdir))
        build_request = BuildRequest(str(tmpdir))
        name_label = "fedora/resultingimage"
        koji_certs_secret_name = 'foobar'
        koji_task_id = 1234
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uri': "example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'koji_task_id': koji_task_id,
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
            'koji_certs_secret': koji_certs_secret_name,
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_json["metadata"]["labels"]["koji-task-id"] == str(koji_task_id)

        plugins = get_plugins_from_build_json(build_json)
        assert get_plugin(plugins, "exit_plugins", "koji_promote")
        assert plugin_value_get(plugins, "exit_plugins", "koji_promote",
                                "args", "kojihub") == kwargs["kojihub"]
        assert plugin_value_get(plugins, "exit_plugins", "koji_promote",
                                "args", "url") == kwargs["openshift_uri"]

        mount_path = get_secret_mountpath_by_name(build_json,
                                                  koji_certs_secret_name)
        assert get_plugin(plugins, 'exit_plugins', 'koji_promote')['args']['koji_ssl_certs'] == mount_path

    def test_render_prod_request_with_koji_kerberos(self, tmpdir):
        self.create_image_change_trigger_json(str(tmpdir))
        build_request = BuildRequest(str(tmpdir))
        name_label = "fedora/resultingimage"
        koji_task_id = 1234
        koji_use_kerberos = True
        koji_kerberos_keytab = "FILE:/tmp/fakekeytab"
        koji_kerberos_principal = "myprincipal@OSBSDOMAIN.COM"
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': "john-foo",
            'component': TEST_COMPONENT,
            'base_image': 'fedora:latest',
            'name_label': name_label,
            'registry_uri': "example.com",
            'openshift_uri': "http://openshift/",
            'builder_openshift_url': "http://openshift/",
            'koji_target': "koji-target",
            'kojiroot': "http://root/",
            'kojihub': "http://hub/",
            'sources_command': "make",
            'koji_task_id': koji_task_id,
            'koji_use_kerberos': koji_use_kerberos,
            'koji_kerberos_keytab': koji_kerberos_keytab,
            'koji_kerberos_principal': koji_kerberos_principal,
            'vendor': "Foo Vendor",
            'authoritative_registry': "registry.example.com",
            'distribution_scope': "authoritative-source-only",
            'registry_api_versions': ['v1'],
        }
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_json["metadata"]["labels"]["koji-task-id"] == str(koji_task_id)

        plugins = get_plugins_from_build_json(build_json)
        assert get_plugin(plugins, "exit_plugins", "koji_promote")
        assert plugin_value_get(plugins, "exit_plugins", "koji_promote",
                                "args", "kojihub") == kwargs["kojihub"]
        assert plugin_value_get(plugins, "exit_plugins", "koji_promote",
                                "args", "url") == kwargs["openshift_uri"]

        assert get_plugin(plugins, 'exit_plugins', 'koji_promote')['args']['koji_principal'] == koji_kerberos_principal
        assert get_plugin(plugins, 'exit_plugins', 'koji_promote')['args']['koji_keytab'] == koji_kerberos_keytab


    @pytest.mark.parametrize(('base_image', 'is_custom'), [
        ('fedora', False),
        ('fedora:latest', False),
        ('koji/image-build', True),
        ('koji/image-build:spam.conf', True),
    ])
    def test_prod_is_custom_base_image(self, tmpdir, base_image, is_custom):
        build_request = BuildRequest(INPUTS_PATH)
        # Safe to call prior to build image being set
        assert build_request.is_custom_base_image() is False

        kwargs = get_sample_prod_params()
        kwargs['base_image'] = base_image
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_request.is_custom_base_image() == is_custom

    def test_prod_missing_kojihub__custom_base_image(self, tmpdir):
        build_request = BuildRequest(INPUTS_PATH)

        kwargs = get_sample_prod_params()
        kwargs['base_image'] = 'koji/image-build'
        del kwargs['kojihub']
        build_request.set_params(**kwargs)

        with pytest.raises(OsbsValidationException) as exc:
            build_request.render()

        assert str(exc.value).startswith(
            'Custom base image builds require kojihub')

    def test_prod_custom_base_image(self, tmpdir):
        build_request = BuildRequest(INPUTS_PATH)

        kwargs = get_sample_prod_params()
        kwargs['base_image'] = 'koji/image-build'
        kwargs['yum_repourls'] = ["http://example.com/my.repo"]
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_request.is_custom_base_image() is True
        plugins = get_plugins_from_build_json(build_json)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, 'prebuild_plugins', 'pull_base_image')

        add_filesystem_args = plugin_value_get(
            plugins, 'prebuild_plugins', 'add_filesystem', 'args')
        assert add_filesystem_args['koji_hub'] == kwargs['kojihub']
        assert add_filesystem_args['koji_proxyuser'] == kwargs['proxy']
        assert add_filesystem_args['repos'] == kwargs['yum_repourls']

    def test_prod_non_custom_base_image(self, tmpdir):
        build_request = BuildRequest(INPUTS_PATH)

        kwargs = get_sample_prod_params()
        build_request.set_params(**kwargs)
        build_json = build_request.render()

        assert build_request.is_custom_base_image() is False
        plugins = get_plugins_from_build_json(build_json)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, 'prebuild_plugins', 'add_filesystem')

        pull_base_image_plugin = get_plugin(
            plugins, 'prebuild_plugins', 'pull_base_image')
        assert pull_base_image_plugin is not None

    def test_render_prod_custom_site_plugin_enable(self):
        """
        Test to make sure that when we attempt to enable a plugin, it is
        actually enabled in the JSON for the build_request after running
        build_request.render()
        """

        plugin_type = "exit_plugins"
        plugin_name = "testing_exit_plugin"
        plugin_args = {"foo": "bar"}

        build_request = BuildRequest(INPUTS_PATH)
        build_request.customize_conf['enable_plugins'].append(
            {
                "plugin_type": plugin_type,
                "plugin_name": plugin_name,
                "plugin_args": plugin_args
            }
        )
        kwargs = get_sample_prod_params()
        build_request.set_params(**kwargs)
        build_request.render()

        assert {
                "name": plugin_name,
                "args": plugin_args
        } in build_request.dj.dock_json[plugin_type]

    def test_render_prod_custom_site_plugin_disable(self):
        """
        Test to make sure that when we attempt to disable a plugin, it is
        actually disabled in the JSON for the build_request after running
        build_request.render()
        """

        plugin_type = "postbuild_plugins"
        plugin_name = "compress"

        build_request = BuildRequest(INPUTS_PATH)
        build_request.customize_conf['disable_plugins'].append(
            {
                "plugin_type": plugin_type,
                "plugin_name": plugin_name
            }
        )
        kwargs = get_sample_prod_params()
        build_request.set_params(**kwargs)
        build_request.render()

        for plugin in build_request.dj.dock_json[plugin_type]:
            if plugin['name'] == plugin_name:
                assert False

    def test_render_prod_custom_site_plugin_override_compress(self):
        """
        Test to make sure that when we attempt to override the compress
        plugin's args, they are actually overridden in the JSON for the
        build_request after running build_request.render() and the plugins
        are in the correct sequential order
        """

        plugin_type = "postbuild_plugins"
        plugin_name = "compress"
        plugin_args = {"foo": "bar"}

        kwargs = get_sample_prod_params()

        unmodified_build_request = BuildRequest(INPUTS_PATH)
        unmodified_build_request.set_params(**kwargs)
        unmodified_build_request.render()

        for plugin_dict in unmodified_build_request.dj.dock_json[plugin_type]:
            if plugin_dict['name'] == plugin_name:
                plugin_index = unmodified_build_request.dj.dock_json[plugin_type].index(plugin_dict)

        build_request = BuildRequest(INPUTS_PATH)
        build_request.customize_conf['enable_plugins'].append(
            {
                "plugin_type": plugin_type,
                "plugin_name": plugin_name,
                "plugin_args": plugin_args
            }
        )
        build_request.set_params(**kwargs)
        build_request.render()

        assert {
                "name": plugin_name,
                "args": plugin_args
        } in build_request.dj.dock_json[plugin_type]

        assert unmodified_build_request.dj.dock_json[plugin_type][plugin_index]['name'] == plugin_name
        assert build_request.dj.dock_json[plugin_type][plugin_index]['name'] == plugin_name

    def test_render_prod_custom_site_plugin_override_tag_and_push(self):
        """
        Test to make sure that when we attempt to override the tag_and_push
        plugin's args, they are actually overridden in the JSON for the
        build_request after running build_request.render() and the plugins
        are in the correct sequential order
        """

        plugin_type = "postbuild_plugins"
        plugin_name = "tag_and_push"
        plugin_args = {"foo": "bar"}
        plugin_args = {
            "registries": {
                "registry.example.com": {
                    "insecure": False,
                    "version": "v2"
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
        build_request.customize_conf['enable_plugins'].append(
            {
                "plugin_type": plugin_type,
                "plugin_name": plugin_name,
                "plugin_args": plugin_args
            }
        )
        build_request.set_params(**kwargs)
        build_request.render()


        assert {
                "name": plugin_name,
                "args": plugin_args
        } in build_request.dj.dock_json[plugin_type]

        assert unmodified_build_request.dj.dock_json[plugin_type][plugin_index]['name'] == plugin_name
        assert build_request.dj.dock_json[plugin_type][plugin_index]['name'] == plugin_name
