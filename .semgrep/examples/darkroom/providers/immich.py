class Client:
    def upload(self, key: str, body: MultipartUpload) -> UploadResult:
        # ok: darkroom-immich-import-routes-behind-check-access
        with self.open("POST", "/api/assets", key, importing=True, upload=body) as response:
            return response

    def bulk_check(self, key: str, sha1: str) -> BulkMatch | None:
        # ok: darkroom-immich-import-routes-behind-check-access
        return self.call("POST", "/api/assets/bulk-upload-check", key=key, importing=True, body={})

    def upload_for_anyone(self, key, body):
        # ruleid: darkroom-immich-import-routes-behind-check-access
        return self.upload(key, body)

    def import_by_hand(self, key):
        # ruleid: darkroom-immich-import-routes-behind-check-access
        return self.call("POST", "/api/assets", key=key, importing=True)


class ManagedReader:
    def upload(self, body, sending):
        self.check_access()
        sending()
        # ok: darkroom-immich-import-routes-behind-check-access
        return self.client.upload(self.key, body)


def upload_without_check_access(client, key, body):
    # ruleid: darkroom-immich-import-routes-behind-check-access
    return client.upload(key, body)
