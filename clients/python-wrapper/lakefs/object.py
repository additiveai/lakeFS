"""
Module containing lakeFS reference implementation
"""

from __future__ import annotations

import base64
import binascii
import io
import os
from typing import (
    AnyStr, AsyncIterable, IO, Iterable, Iterator,
    List, Literal, NamedTuple, Optional, TypeVar, Union,
    get_args)

import lakefs_sdk
from lakefs_sdk import StagingMetadata

from lakefs.client import Client, DEFAULT_CLIENT
from lakefs.exceptions import (
    api_exception_handler,
    LakeFSException,
    NotFoundException,
    ObjectNotFoundException,
    NotAuthorizedException,
    ForbiddenException,
    PermissionException,
    ObjectExistsException
)

_LAKEFS_METADATA_PREFIX = "x-lakefs-meta-"

# Type to support both strings and bytes in addition to streams (reference: httpx._types.RequestContent)
UploadContentType = Union[str, bytes, Iterable[bytes], AsyncIterable[bytes]]
OpenModes = Literal['r', 'rb']
WriteModes = Literal['x', 'xb', 'w', 'wb']


class ObjectStats(NamedTuple):
    """
    Represent a lakeFS object's stats
    """
    path: str
    path_type: str
    physical_address: str
    checksum: str
    mtime: int
    physical_address_expiry: Optional[int] = None
    size_bytes: Optional[int] = None
    metadata: Optional[dict[str, str]] = None
    content_type: Optional[str] = None


class StoredObject:
    """
    Class representing an object in lakeFS.
    """
    _client: Client
    _repo_id: str
    _ref_id: str
    _path: str
    _stats: Optional[ObjectStats] = None

    def __init__(self, repository: str, reference: str, path: str, client: Optional[Client] = DEFAULT_CLIENT):
        self._client = client
        self._repo_id = repository
        self._ref_id = reference
        self._path = path

    @property
    def repo(self) -> str:
        """
        Returns the object's repository id
        """
        return self._repo_id

    @property
    def ref(self) -> str:
        """
        Returns the object's reference id
        """
        return self._ref_id

    @property
    def path(self) -> str:
        """
        Returns the object's path relative to repository and reference ids
        """
        return self._path

    def open(self, mode: OpenModes = 'r', pre_sign: Optional[bool] = None) -> ObjectReader:
        """
        Context manager which provide a file-descriptor like object that allow reading the given object
        :param mode: Open mode - as supported by OpenModes
        :param pre_sign: (Optional), enforce the pre_sign mode on the lakeFS server. If not set, will probe server for
        information.
        :return: A Reader object
        """
        reader = ObjectReader(self, mode=mode, pre_sign=pre_sign, client=self._client)
        return reader

    def stat(self) -> ObjectStats:
        """
        Return the Stat object representing this object
        """
        if self._stats is None:
            with api_exception_handler(_io_exception_handler):
                stat = self._client.sdk_client.objects_api.stat_object(self._repo_id, self._ref_id, self._path)
                self._stats = ObjectStats(**stat.__dict__)
        return self._stats

    def exists(self) -> bool:
        """
        Returns True if object exists in lakeFS, False otherwise
        """

        exists = False

        def exist_handler(e: LakeFSException):
            if isinstance(e, NotFoundException):
                return None  # exists = False
            return _io_exception_handler(e)

        with api_exception_handler(exist_handler):
            self._client.sdk_client.objects_api.head_object(self._repo_id, self._ref_id, self._path)
            exists = True

        return exists

    def copy(self, destination_branch_id: str, destination_path: str) -> WriteableObject:
        """
        Copy the object to a destination branch
        :param destination_branch_id: The destination branch to copy the object to
        :param destination_path: The path of the copied object in the destination branch
        :return: The newly copied Object
        :raises:
            ObjectNotFoundException if repo id,reference id, destination branch id or object path does not exist
            PermissionException if user is not authorized to perform this operation, or operation is forbidden
            ServerException for any other errors
        """

        with api_exception_handler():
            object_copy_creation = lakefs_sdk.ObjectCopyCreation(src_ref=self._ref_id, src_path=self._path)
            self._client.sdk_client.objects_api.copy_object(repository=self._repo_id,
                                                            branch=destination_branch_id,
                                                            dest_path=destination_path,
                                                            object_copy_creation=object_copy_creation)

        return WriteableObject(repository=self._repo_id, reference=destination_branch_id, path=destination_path,
                               client=self._client)


class ObjectReader(IO):
    """
    ReadableObject provides read-only functionality for lakeFS objects with IO semantics.
    This Object is instantiated and returned on open() methods for immutable reference types (Commit, Tag...)
    """
    _client: Client
    _obj: StoredObject
    _mode: OpenModes
    _pos: int
    _pre_sign: Optional[bool] = None
    _is_closed: bool = False

    def __init__(self, obj: StoredObject, mode: OpenModes, pre_sign: Optional[bool] = None,
                 client: Optional[Client] = DEFAULT_CLIENT) -> None:
        if mode not in get_args(OpenModes):
            raise ValueError(f"invalid read mode: '{mode}'. ReadModes: {OpenModes}")

        self._obj = obj
        self._mode = mode
        self._pre_sign = pre_sign if pre_sign is not None else client.storage_config.pre_sign_support
        self._client = client
        self._pos = 0

    #  typing.IO Interface Implementation ############################################################

    @property
    def mode(self) -> str:
        """
        Returns the open mode for this object
        """
        return self._mode

    @property
    def name(self) -> str:
        """
        Returns the name of the object relative to the repo and reference
        """
        return self._obj.path

    @property
    def closed(self) -> bool:
        """
        Returns True after the object is closed
        """
        return self._is_closed

    def close(self) -> None:
        """
        Closes the current file descriptor for IO operations
        """
        self._is_closed = True

    def fileno(self) -> int:
        """
        The file descriptor number as defined by the operating system. In the context of lakeFS it has no meaning

        :return: -1 Always
        """
        return -1

    def flush(self) -> None:
        """
        Irrelevant for the lakeFS implementation
        """

    def isatty(self) -> bool:
        """
        Irrelevant for the lakeFS implementation
        """
        return False

    def readable(self) -> bool:
        """
        Returns True always
        """
        return True

    def readline(self, limit: int = -1):
        """
        Currently unsupported
        """
        raise io.UnsupportedOperation

    def readlines(self, hint: int = -1):
        """
        Currently unsupported
        """
        raise io.UnsupportedOperation

    def seekable(self) -> bool:
        """
        Returns True always
        """
        return True

    def truncate(self, size: int = None) -> int:
        """
        Unsupported by lakeFS implementation
        """
        raise io.UnsupportedOperation

    def writable(self) -> bool:
        """
        Unsupported - read only object
        """
        return False

    def write(self, s: AnyStr) -> int:
        """
        Unsupported - read only object
        """
        raise io.UnsupportedOperation

    def writelines(self, lines: List[AnyStr]) -> None:
        """
        Unsupported by lakeFS implementation
        """
        raise io.UnsupportedOperation

    def __next__(self) -> AnyStr:
        """
        Unsupported by lakeFS implementation
        """
        raise io.UnsupportedOperation

    def __iter__(self) -> Iterator[AnyStr]:
        """
        Unsupported by lakeFS implementation
        """
        raise io.UnsupportedOperation

    def __enter__(self) -> TypeVar("Self", bound="ObjectReader"):
        return self

    def __exit__(self, typ, value, traceback) -> bool:
        self.close()
        return False  # Don't suppress an exception

    def seek(self, offset: int, whence: int = 0) -> int:
        """
        Move the object's reading position
        :param offset: The offset from the beginning of the file
        :param whence: Optional. The whence argument is optional and defaults to
            os.SEEK_SET or 0 (absolute file positioning);
            other values are os.SEEK_CUR or 1 (seek relative to the current position) and os.SEEK_END or 2
            (seek relative to the file’s end)
            os.SEEK_END is not supported
        :raises OSError if calculated new position is negative
        """
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = offset - whence
        else:
            raise io.UnsupportedOperation(f"whence={whence} is not supported")

        if pos < 0:
            raise OSError("position must be a non-negative integer")
        self._pos = pos
        return pos

    def read(self, n: int = None) -> str | bytes:
        """
        Read object data
        :param n: How many bytes to read. If read_bytes is None, will read from current position to end.
        If current position + read_bytes > object size.
        :return: The bytes read
        :raises
            EOFError if current position is after object size
            OSError if read_bytes is non-positive
            ObjectNotFoundException if repo id, reference id or object path does not exist
            PermissionException if user is not authorized to perform this operation, or operation is forbidden
            ServerException for any other errors
        """
        if n and n <= 0:
            raise OSError("read_bytes must be a positive integer")

        read_range = self._get_range_string(start=self._pos, read_bytes=n)
        with api_exception_handler(_io_exception_handler):
            contents = self._client.sdk_client.objects_api.get_object(self._obj.repo,
                                                                      self._obj.ref,
                                                                      self._obj.path,
                                                                      range=read_range,
                                                                      presign=self._pre_sign)
        self._pos += len(contents)  # Update pointer position
        if 'b' not in self._mode:
            return contents.decode('utf-8')

        return contents

    #  END typing.IO Interface Implementation ############################################################

    def tell(self) -> int:
        """
        Object's read position. If position is past object byte size, trying to read the object will return EOF
        """
        return self._pos

    @staticmethod
    def _get_range_string(start, read_bytes=None):
        if start == 0 and read_bytes is None:
            return None
        if read_bytes is None:
            return f"bytes={start}-"
        return f"bytes={start}-{start + read_bytes - 1}"


class WriteableObject(StoredObject):
    """
    WriteableObject inherits from ReadableObject and provides read/write functionality for lakeFS objects
    using IO semantics.
    This Object is instantiated and returned upon invoking open() on Branch reference type.
    """

    def __init__(self, repository: str, reference: str, path: str, client: Optional[Client] = DEFAULT_CLIENT) -> None:
        super().__init__(repository, reference, path, client=client)

    def create(self,
               data: UploadContentType,
               mode: WriteModes = 'wb',
               pre_sign: Optional[bool] = None,
               content_type: Optional[str] = None,
               metadata: Optional[dict[str, str]] = None) -> WriteableObject:
        """
        Creates a new object or overwrites an existing object
        :param data: The contents of the object to write (can be bytes or string)
        :param mode: Write mode:
            'x'     - Open for exclusive creation
            'xb'    - Open for exclusive creation in binary mode
            'w'     - Create a new object or truncate if exists
            'wb'    - Create or truncate in binary mode
        :param pre_sign: (Optional) Explicitly state whether to use pre_sign mode when uploading the object.
        If None, will be taken from pre_sign property.
        :param content_type: (Optional) Explicitly set the object Content-Type
        :param metadata: (Optional) User metadata
        :return: The Stat object representing the newly created object
        :raises
            ObjectExistsException if object exists and mode is exclusive ('x')
            ObjectNotFoundException if repo id, reference id or object path does not exist
            PermissionException if user is not authorized to perform this operation, or operation is forbidden
            ServerException for any other errors
        """
        if mode not in get_args(WriteModes):
            raise ValueError(f"invalid write mode: '{mode}'. WriteModes: {WriteModes}")

        if 'x' in mode and self.exists():  # Requires explicit create
            raise ObjectExistsException

        # TODO: handle streams
        binary_mode = 'b' in mode
        if binary_mode and isinstance(data, str):
            content = data.encode('utf-8')
        elif not binary_mode and isinstance(data, bytes):
            content = data.decode('utf-8')
        else:
            content = data

        is_presign = pre_sign if pre_sign is not None else self._client.storage_config.pre_sign_support
        with api_exception_handler(_io_exception_handler):
            stats = self._upload_presign(content, content_type, metadata) if is_presign \
                else self._upload_raw(content, content_type, metadata)
            self._stats = ObjectStats(**stats.__dict__)

        return self

    @staticmethod
    def _extract_etag_from_response(headers) -> str:
        # prefer Content-MD5 if exists
        content_md5 = headers.get("Content-MD5")
        if content_md5 is not None and len(content_md5) > 0:
            try:  # decode base64, return as hex
                decode_md5 = base64.b64decode(content_md5)
                return binascii.hexlify(decode_md5).decode("utf-8")
            except binascii.Error:
                pass

        # fallback to ETag
        etag = headers.get("ETag", "").strip(' "')
        return etag

    def _upload_raw(self, content: UploadContentType, content_type: Optional[str] = None,
                    metadata: Optional[dict[str, str]] = None) -> ObjectStats:
        """
        Use raw upload API call to bypass validation of content parameter
        """
        headers = {
            "Accept": "application/json",
            "Content-Type": content_type if content_type is not None else "application/octet-stream"
        }

        # Create user metadata headers
        if metadata is not None:
            for k, v in metadata.items():
                headers[_LAKEFS_METADATA_PREFIX + k] = v

        _response_types_map = {
            '201': "ObjectStats",
            '400': "Error",
            '401': "Error",
            '403': "Error",
            '404': "Error",
            '412': "Error",
            '420': None,
        }
        # authentication setting
        _auth_settings = ['basic_auth', 'cookie_auth', 'oidc_auth', 'saml_auth', 'jwt_token']
        return self._client.sdk_client.objects_api.api_client.call_api(
            resource_path='/repositories/{repository}/branches/{branch}/objects',
            method='POST',
            path_params={"repository": self._repo_id, "branch": self._ref_id},
            query_params={"path": self._path},
            header_params=headers,
            body=content,
            response_types_map=_response_types_map,
            auth_settings=_auth_settings,
            _return_http_data_only=True
        )

    def _upload_presign(self, content: UploadContentType, content_type: Optional[str] = None,
                        metadata: Optional[dict[str, str]] = None) -> ObjectStats:
        headers = {
            "Accept": "application/json",
            "Content-Type": content_type if content_type is not None else "application/octet-stream"
        }

        staging_location = self._client.sdk_client.staging_api.get_physical_address(self._repo_id,
                                                                                    self._ref_id,
                                                                                    self._path,
                                                                                    True)
        url = staging_location.presigned_url
        if self._client.storage_config.blockstore_type == "azure":
            headers["x-ms-blob-type"] = "BlockBlob"

        resp = self._client.sdk_client.objects_api.api_client.request(
            method="PUT",
            url=url,
            body=content,
            headers=headers
        )

        etag = WriteableObject._extract_etag_from_response(resp.getheaders())
        size_bytes = len(content.encode('utf-8')) if isinstance(content, str) else len(content)
        staging_metadata = StagingMetadata(staging=staging_location,
                                           size_bytes=size_bytes,
                                           checksum=etag,
                                           user_metadata=metadata)
        return self._client.sdk_client.staging_api.link_physical_address(self._repo_id, self._ref_id, self._path,
                                                                         staging_metadata=staging_metadata)

    def delete(self) -> None:
        """
        Delete object from lakeFS
            ObjectNotFoundException if repo id, reference id or object path does not exist
            PermissionException if user is not authorized to perform this operation, or operation is forbidden
            ServerException for any other errors
        """
        with api_exception_handler(_io_exception_handler):
            self._client.sdk_client.objects_api.delete_object(self._repo_id, self._ref_id, self._path)
            self._stats = None


def _io_exception_handler(e: LakeFSException):
    if isinstance(e, NotFoundException):
        return ObjectNotFoundException(e.status_code, e.reason)
    if isinstance(e, (NotAuthorizedException, ForbiddenException)):
        return PermissionException(e.status_code, e.reason)
    return e
