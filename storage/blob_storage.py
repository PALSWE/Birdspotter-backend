import json
from typing import Optional

from azure.storage.blob import BlobServiceClient, ContentSettings


class BlobStorage:
    def __init__(
        self,
        connection_string: str,
        observations_container_name: str,
        metadata_container_name: str,
    ):
        service_client = BlobServiceClient.from_connection_string(connection_string)

        self.observations_container = service_client.get_container_client(
            observations_container_name
        )
        self.metadata_container = service_client.get_container_client(
            metadata_container_name
        )

        if not self.observations_container.exists():
            self.observations_container.create_container()

        if not self.metadata_container.exists():
            self.metadata_container.create_container()

    def _container_for_filename(self, filename: str):
        if filename == "syncstate.json":
            return self.metadata_container

        return self.observations_container

    def read_json(self, filename: str) -> Optional[dict]:
        container = self._container_for_filename(filename)
        blob_client = container.get_blob_client(filename)

        if not blob_client.exists():
            return None

        content = blob_client.download_blob().readall().decode("utf-8")
        return json.loads(content)

    def write_json(self, filename: str, payload: dict, *, pretty: bool = True) -> None:
        container = self._container_for_filename(filename)

        content = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )

        container.upload_blob(
            name=filename,
            data=content,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

    def read_text(self, filename: str) -> Optional[str]:
        container = self._container_for_filename(filename)
        blob_client = container.get_blob_client(filename)

        if not blob_client.exists():
            return None

        return blob_client.download_blob().readall().decode("utf-8")
