import json
import logging
from typing import List, Dict, Any

import ckanapi
from fastapi import HTTPException
from pydantic import AnyHttpUrl
from requests import RequestException
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from odp.api.models import Pagination
from odp.api.models.collection import Collection, CollectionIn, COLLECTION_SUFFIX
from odp.api.models.metadata import (
    MetadataRecord,
    MetadataRecordIn,
    MetadataValidationResult,
    MetadataWorkflowResult,
)
from odp.api.models.project import Project, PROJECT_SUFFIX
from odp.api.public.adapter import ODPAPIAdapter, ODPAPIAdapterConfig

logger = logging.getLogger(__name__)


class CKANAdapterConfig(ODPAPIAdapterConfig):
    """
    Config for the CKAN adapter, populated from the environment.
    """
    CKAN_URL: AnyHttpUrl

    class Config:
        env_prefix = 'CKAN_ADAPTER.'


class CKANAdapter(ODPAPIAdapter):

    def __init__(self, app, config: CKANAdapterConfig):
        super().__init__(app, config)
        self.ckan_server_url = config.CKAN_URL

    def _call_ckan(self, action, access_token, **kwargs):
        """
        Call a CKAN API action function.

        :param action: CKAN action function name
        :param access_token: the access token string to be forwarded to CKAN in the Authorization header
        :param kwargs: parameters to populate the data_dict for the action function
        :returns: the response dictionary / value returned from CKAN
        :raises HTTPException
        """
        try:
            with ckanapi.RemoteCKAN(self.ckan_server_url) as ckan:
                return ckan.call_action(
                    action,
                    data_dict=kwargs,
                    apikey=f'Bearer {access_token}',
                    requests_kwargs={
                        'verify': self.app_config.SERVER_ENV != 'development'
                    },
                )

        except RequestException as e:
            raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE,
                                detail=f"Error sending request to CKAN: {e}") from e

        except ckanapi.ValidationError as e:
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                                detail=f"CKAN validation error: {e}") from e

        except ckanapi.NotAuthorized as e:
            raise HTTPException(status_code=HTTP_403_FORBIDDEN,
                                detail=f"Not authorized to access CKAN resource: {e}") from e

        except ckanapi.NotFound as e:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                                detail=f"CKAN resource not found: {e}") from e

        except ckanapi.CKANAPIError as e:
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                                detail=f"CKAN error: {e}") from e

    @staticmethod
    def _translate_from_ckan_record(ckan_record: dict) -> MetadataRecord:
        """
        Convert a CKAN metadata record dict into a MetadataRecord object.
        """
        return MetadataRecord(
            institution_key=ckan_record['owner_org'],
            collection_key=ckan_record['metadata_collection_id'],
            schema_key=ckan_record['metadata_standard_id'],
            metadata=ckan_record['metadata_json'],
            id=ckan_record['id'],
            pid=ckan_record['name'] if ckan_record['name'] != ckan_record['id'] else None,
            doi=ckan_record['doi'],
            validated=ckan_record['validated'],
            errors=ckan_record['errors'],
            state=ckan_record['workflow_state_id'],
        )

    @staticmethod
    def _translate_to_ckan_record(institution_key: str, metadata_record: MetadataRecordIn) -> dict:
        """
        Convert a MetadataRecordIn object into a CKAN metadata record dict.
        """
        return {
            'owner_org': institution_key,
            'metadata_collection_id': metadata_record.collection_key,
            'metadata_standard_id': metadata_record.schema_key,
            'metadata_json': json.dumps(metadata_record.metadata),
            'doi': metadata_record.doi,
            'auto_assign_doi': metadata_record.auto_assign_doi,
        }

    def list_metadata_records(self,
                              institution_key: str,
                              pagination: Pagination,
                              access_token: str,
                              ) -> List[MetadataRecord]:
        ckan_record_list = self._call_ckan(
            'metadata_record_list',
            access_token,
            owner_org=institution_key,
            offset=pagination.offset,
            limit=pagination.limit,
            all_fields=True,
            deserialize_json=True,
        )
        return [self._translate_from_ckan_record(record) for record in ckan_record_list]

    def get_metadata_record(self,
                            institution_key: str,
                            record_id: str,
                            access_token: str,
                            ) -> MetadataRecord:
        ckan_record = self._call_ckan(
            'metadata_record_show',
            access_token,
            id=record_id,
            deserialize_json=True,
        )
        # check that the record has not been marked as deleted in CKAN, and
        # that it belongs to the given institution
        if ckan_record['state'] != 'active' or ckan_record['owner_org'] != institution_key:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="CKAN resource not found")

        return self._translate_from_ckan_record(ckan_record)

    def create_or_update_metadata_record(self,
                                         institution_key: str,
                                         metadata_record: MetadataRecordIn,
                                         access_token: str,
                                         ) -> MetadataRecord:
        input_dict = self._translate_to_ckan_record(institution_key, metadata_record)
        ckan_record = self._call_ckan(
            'metadata_record_create',
            access_token,
            deserialize_json=True,
            **input_dict,
        )
        record_id = ckan_record['id']
        self._annotate_metadata_record(record_id, metadata_record, access_token)

        if not ckan_record['validated']:
            # try to validate the record, for convenience
            try:
                validaton_result = self.validate_metadata_record(institution_key, record_id, access_token)
                ckan_record.update({
                    'validated': True,
                    'errors': validaton_result.errors,
                })
            except HTTPException as e:
                # note: this is a system error, not a validation error
                logger.warning(f"An error occurred while validating metadata record {record_id}: {e.detail}")

        return self._translate_from_ckan_record(ckan_record)

    def update_metadata_record(self,
                               institution_key: str,
                               record_id: str,
                               metadata_record: MetadataRecordIn,
                               access_token: str,
                               ) -> MetadataRecord:
        # make sure that the record we're updating belongs to the specified institution
        self.get_metadata_record(institution_key, record_id, access_token)

        input_dict = self._translate_to_ckan_record(institution_key, metadata_record)
        ckan_record = self._call_ckan(
            'metadata_record_update',
            access_token,
            id=record_id,
            deserialize_json=True,
            **input_dict,
        )
        self._annotate_metadata_record(record_id, metadata_record, access_token)

        if not ckan_record['validated']:
            # try to validate the record, for convenience
            try:
                validaton_result = self.validate_metadata_record(institution_key, record_id, access_token)
                ckan_record.update({
                    'validated': True,
                    'errors': validaton_result.errors,
                })
            except HTTPException as e:
                # note: this is a system error, not a validation error
                logger.warning(f"An error occurred while validating metadata record {record_id}: {e.detail}")

        return self._translate_from_ckan_record(ckan_record)

    def delete_metadata_record(self,
                               institution_key: str,
                               record_id: str,
                               access_token: str,
                               ) -> bool:
        # make sure that the record we're deleting belongs to the specified institution
        self.get_metadata_record(institution_key, record_id, access_token)

        self._call_ckan(
            'metadata_record_delete',
            access_token,
            id=record_id,
        )
        return True

    def validate_metadata_record(self,
                                 institution_key: str,
                                 record_id: str,
                                 access_token: str,
                                 ) -> MetadataValidationResult:
        # make sure that the record we're validating belongs to the specified institution
        self.get_metadata_record(institution_key, record_id, access_token)

        validation_activity_record = self._call_ckan(
            'metadata_record_validate',
            access_token,
            id=record_id,
        )
        validation_results = validation_activity_record['data']['results']
        validation_errors = {}
        for validation_result in validation_results:
            validation_errors.update(validation_result['errors'])

        return MetadataValidationResult(
            success=not validation_errors,
            errors=validation_errors,
        )

    def change_state_of_metadata_record(self,
                                        institution_key: str,
                                        record_id: str,
                                        state: str,
                                        access_token: str,
                                        ) -> MetadataWorkflowResult:
        # make sure that the record we're updating belongs to the specified institution
        self.get_metadata_record(institution_key, record_id, access_token)

        workflow_activity_record = self._call_ckan(
            'metadata_record_workflow_state_transition',
            access_token,
            id=record_id,
            workflow_state_id=state,
        )
        workflow_errors = workflow_activity_record['data']['errors']

        return MetadataWorkflowResult(
            success=not workflow_errors,
            errors=workflow_errors,
        )

    def _annotate_metadata_record(self,
                                  record_id: str,
                                  metadata_record: MetadataRecordIn,
                                  access_token: str,
                                  ) -> None:

        def annotate(key: str, value: Dict[str, Any]):
            try:
                self._call_ckan('metadata_record_workflow_annotation_create', access_token,
                                id=record_id, key=key, value=json.dumps(value))
            except HTTPException as e:
                err = None
                if e.status_code == 400:
                    # if we are doing a metadata record update, create annotation may fail with a 400 (duplicate),
                    # so we try updating the annotation
                    try:
                        self._call_ckan('metadata_record_workflow_annotation_update', access_token,
                                        id=record_id, key=key, value=json.dumps(value))
                    except HTTPException as e:
                        err = e.detail
                else:
                    err = e.detail

                if err:
                    logger.error(f'Error setting "{key}" annotation on metadata record {record_id}: {err}')

        annotate(
            key='terms_and_conditions',
            value={'accepted': metadata_record.terms_conditions_accepted},
        )
        annotate(
            key='data_agreement',
            value={'accepted': metadata_record.data_agreement_accepted, 'href': metadata_record.data_agreement_url},
        )
        annotate(
            key='capture_info',
            value={'capture_method': metadata_record.capture_method},
        )

    @staticmethod
    def _translate_from_ckan_collection(ckan_collection: dict) -> Collection:
        """
        Convert a CKAN collection dict into a Collection.
        """
        return Collection(
            institution_key=ckan_collection['organization_id'],
            key=ckan_collection['name'],
            name=ckan_collection['title'],
            description=ckan_collection['description'],
            doi_scope=ckan_collection['doi_collection'],
            project_keys=[project_dict['id'] for project_dict in ckan_collection['infrastructures']]
        )

    @staticmethod
    def _translate_to_ckan_collection(institution_key: str, collection: CollectionIn) -> dict:
        """
        Convert a CollectionIn into a CKAN collection dict.
        """
        return {
            'name': collection.key,
            'title': collection.name,
            'description': collection.description,
            'organization_id': institution_key,
            'doi_collection': collection.doi_scope,
            'infrastructures': [{'id': key} for key in collection.project_keys],
        }

    def list_collections(self,
                         institution_key: str,
                         access_token: str,
                         ) -> List[Collection]:
        collection_list = self._call_ckan(
            'metadata_collection_list',
            access_token,
            owner_org=institution_key,
            all_fields=True,
        )
        return [self._translate_from_ckan_collection(collection) for collection in collection_list]

    def create_collection(self,
                          institution_key: str,
                          collection: CollectionIn,
                          access_token: str,
                          ) -> Collection:
        # we do this because in CKAN, organizations, collections and collections share
        # the same key namespace, by virtue of them all being CKAN groups
        if not collection.key.endswith(COLLECTION_SUFFIX):
            collection.key += COLLECTION_SUFFIX

        input_dict = self._translate_to_ckan_collection(institution_key, collection)
        ckan_collection = self._call_ckan(
            'metadata_collection_create',
            access_token,
            **input_dict,
        )
        return self._translate_from_ckan_collection(ckan_collection)

    @staticmethod
    def _translate_from_ckan_project(ckan_project: dict) -> Project:
        """
        Convert a CKAN project (infrastructure) dict into a Project.
        """
        return Project(
            key=ckan_project['name'],
            name=ckan_project['title'],
            description=ckan_project['description'],
        )

    @staticmethod
    def _translate_to_ckan_project(project: Project) -> dict:
        """
        Convert a Project into a CKAN project (infrastructure) dict.
        """
        return {
            'name': project.key,
            'title': project.name,
            'description': project.description,
        }

    def list_projects(self,
                      access_token: str,
                      ) -> List[Project]:
        project_list = self._call_ckan(
            'infrastructure_list',
            access_token,
            all_fields=True,
        )
        return [self._translate_from_ckan_project(project) for project in project_list]

    def create_or_update_project(self,
                                 project: Project,
                                 access_token: str,
                                 ) -> Project:
        # we do this because in CKAN, organizations, collections and projects share
        # the same key namespace, by virtue of them all being CKAN groups
        if not project.key.endswith(PROJECT_SUFFIX):
            project.key += PROJECT_SUFFIX

        input_dict = self._translate_to_ckan_project(project)
        try:
            ckan_project = self._call_ckan(
                'infrastructure_create',
                access_token,
                **input_dict,
            )
        except HTTPException as e:
            if e.status_code == HTTP_400_BAD_REQUEST and 'Group name already exists in database' in e.detail:
                input_dict['id'] = input_dict['name']
                ckan_project = self._call_ckan(
                    'infrastructure_update',
                    access_token,
                    **input_dict,
                )
            else:
                raise

        return self._translate_from_ckan_project(ckan_project)
