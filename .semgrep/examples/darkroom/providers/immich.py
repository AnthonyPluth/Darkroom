class Client:
    def open(self, method: str, path: str, key: str, body: object | None = None, *, importing: bool = False,
             upload: MultipartUpload | None = None) -> contextlib.AbstractContextManager[Any]:
        # ok: darkroom-immich-import-routes-behind-check-access
        require_allowed_request(method, path, importing=importing)

    def call(self, method: str, path: str, keyed: bool = True, key: str | None = None, body: object | None = None, *, importing: bool = False) -> object:
        # ok: darkroom-immich-import-routes-behind-check-access
        with self.open(method, path, key, body, importing=importing) as response:
            return response

    def forward_a_flag(self, key, importing):
        # ruleid: darkroom-immich-import-routes-behind-check-access
        return self.call("POST", "/api/assets", key=key, importing=importing)

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

    def upload_without_checking(self, body):
        # ruleid: darkroom-immich-import-routes-behind-check-access
        return self.client.upload(self.key, body)

    def check_after_sending(self, sha1):
        # ruleid: darkroom-immich-import-routes-behind-check-access
        found = self.client.bulk_check(self.key, sha1)
        self.check_access()
        return found


def upload_without_check_access(client, key, body):
    # ruleid: darkroom-immich-import-routes-behind-check-access
    return client.upload(key, body)
