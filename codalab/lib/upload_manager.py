import os
import shutil
import tempfile
import tarfile

from apache_beam.io.filesystem import CompressionTypes
from apache_beam.io.filesystems import FileSystems
from io import BytesIO
from ratarmount import SQLiteIndexedTar
from typing import Optional, List, Union, Tuple, IO, cast

from codalab.common import UsageError
from codalab.common import StorageType
from codalab.lib import crypt_util, file_util, path_util
from codalab.worker.file_util import tar_gzip_directory
from codalab.worker.tar_from_sources import TarFromSources
from codalab.objects.bundle import Bundle

Source = Union[str, Tuple[str, IO[bytes]]]


class UploadManager(object):
    """
    Contains logic for uploading bundle data to the bundle store and updating
    the associated bundle metadata in the database.
    """

    def __init__(self, bundle_model, bundle_store):
        from codalab.lib import zip_util

        # exclude these patterns by default
        self._bundle_model = bundle_model
        self._bundle_store = bundle_store
        self.zip_util = zip_util

    def upload_to_bundle_store(
        self,
        bundle: Bundle,
        sources: List[Source],
        git: bool,
        unpack: bool,
        simplify_archives: bool,
        use_azure_blob_beta: bool,
    ):
        """
        Uploads contents for the given bundle to the bundle store.

        |sources|: specifies the locations of the contents to upload. Each element is
                   either a URL or a tuple (filename, binary file-like object).
        |git|: for URLs, whether |source| is a git repo to clone.
        |unpack|: for each source in |sources|, whether to unpack it if it's an archive.
        |simplify_archives|: whether to simplify unpacked archives so that if they
                             contain a single file, the final path is just that file,
                             not a directory containing that file.
        |use_azure_blob_beta|: whether to use Azure Blob Storage.

        If |sources| contains one source, then the bundle contents will be that source.
        Otherwise, the bundle contents will be a directory with each of the sources.
        Exceptions:
        - If |git|, then each source is replaced with the result of running 'git clone |source|'
        - If |unpack| is True or a source is an archive (zip, tar.gz, etc.), then unpack the source.
        """
        if use_azure_blob_beta:
            return self._upload_to_bundle_store_blob(
                bundle, sources, git, unpack, simplify_archives
            )
        else:
            return self._upload_to_bundle_store_disk(
                bundle, sources, git, unpack, simplify_archives
            )

    def _upload_to_bundle_store_disk(
        self,
        bundle: Bundle,
        sources: List[Source],
        git: bool,
        unpack: bool,
        simplify_archives: bool,
    ):
        """Upload to a regular disk bundle store. Files are unpacked to
        files / directories and then saved on bundle's subdirectory on the
        disk bundle store."""
        bundle_path = self._bundle_store.get_bundle_location(bundle.uuid)
        try:
            path_util.make_directory(bundle_path)
            # Note that for uploads with a single source, the directory
            # structure is simplified at the end.
            for source in sources:
                is_url, is_fileobj, filename = self._interpret_source(source)
                source_output_path = os.path.join(bundle_path, filename)
                if is_url:
                    assert isinstance(source, str)
                    if git:
                        source_output_path = file_util.strip_git_ext(source_output_path)
                        file_util.git_clone(source, source_output_path)
                    else:
                        file_util.download_url(source, source_output_path)
                        if unpack and self._can_unpack_file(source_output_path):
                            self._unpack_file(
                                source_output_path,
                                self.zip_util.strip_archive_ext(source_output_path),
                                remove_source=True,
                                simplify_archive=simplify_archives,
                            )
                elif is_fileobj:
                    if unpack and self.zip_util.path_is_archive(filename):
                        self._unpack_fileobj(
                            source[0],
                            source[1],
                            self.zip_util.strip_archive_ext(source_output_path),
                            simplify_archive=simplify_archives,
                        )
                    else:
                        # We reach this code path if we are uploading a single file regularly.
                        with open(source_output_path, 'wb') as out:
                            shutil.copyfileobj(cast(IO, source[1]), out)

            if len(sources) == 1:
                self._simplify_directory(bundle_path)

            # is_directory is True if the bundle is a directory and False if it is a single file.
            is_directory = os.path.isdir(bundle_path)
            self._bundle_model.update_bundle(
                bundle, {'storage_type': StorageType.DISK_STORAGE.value, 'is_dir': is_directory},
            )
        except UsageError:
            if os.path.exists(bundle_path):
                path_util.remove(bundle_path)
            raise

    def _upload_to_bundle_store_blob(
        self,
        bundle: Bundle,
        sources: List[Source],
        git: bool,
        unpack: bool,
        simplify_archives: bool,
    ):
        """Upload to a Blob Storage bundle store. Files are unpacked, streaming,
        directly to a .tar.gz file that is then stored in the bundle's subdirectory
        on Blob Storage. Finally, an index is created for the .tar.gz file on
        Blob Storage."""
        bundle_path = self._bundle_store.get_bundle_location(
            bundle.uuid
        )  # this path will end in contents.tar.gz
        try:
            with FileSystems.create(
                bundle_path, compression_type=CompressionTypes.UNCOMPRESSED,
            ) as out:
                archive_file = TarFromSources(fileobj=out, mode="w:gz", bundle_uuid=bundle.uuid)
                for source in sources:
                    is_url, is_fileobj, filename = self._interpret_source(source)
                    if unpack and self.zip_util.path_is_archive(filename):
                        # Add an archive
                        source_input_fileobj = archive_file.add_source(
                            self.zip_util.strip_archive_ext(filename),
                            archive_ext=self.zip_util.get_archive_ext(filename),
                            simplify_archives=simplify_archives,
                        )
                    else:
                        # Add a single file
                        source_input_fileobj = archive_file.add_source(filename)
                    if is_url:
                        assert isinstance(source, str)
                        if git:
                            with tempfile.TemporaryDirectory() as tmp:
                                file_util.git_clone(source, tmp)
                                tar_gzip_directory(tmp, stdout=source_input_fileobj)
                        else:
                            file_util.download_url(source, out_file=source_input_fileobj)
                    elif is_fileobj:
                        shutil.copyfileobj(cast(IO, source[1]), source_input_fileobj)
                    # Read the entire file object so it gets written to the file, then close it.
                    for _ in source_input_fileobj:
                        pass
                    source_input_fileobj.close()
                archive_file.close()

                self._bundle_model.update_bundle(
                    bundle,
                    {
                        'storage_type': StorageType.AZURE_BLOB_STORAGE.value,
                        'is_dir': archive_file.is_directory,
                    },
                )
                # Now, upload the contents of the temp directory to Azure Blob Storage.
                bundle_url = self._bundle_store.get_bundle_location(bundle.uuid)
                with FileSystems.open(
                    bundle_url, compression_type=CompressionTypes.UNCOMPRESSED
                ) as ttf, tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp_index_file:
                    # Write index file to tmp_index_file.
                    SQLiteIndexedTar(
                        fileObject=ttf,
                        tarFileName=bundle.uuid,
                        writeIndex=True,
                        clearIndexCache=True,
                        indexFileName=tmp_index_file.name,
                    )
                    # Write index file to Azure Blob Storage.
                    with FileSystems.create(
                        bundle_url.replace("/contents.tar.gz", "/index.sqlite"),
                        compression_type=CompressionTypes.UNCOMPRESSED,
                    ) as out_index_file, open(tmp_index_file.name, "rb") as tif:
                        shutil.copyfileobj(tif, out_index_file)
        except UsageError:
            path_util.remove(bundle_path)
            raise

    def _interpret_source(self, source: Source):
        is_url, is_fileobj = False, False
        if isinstance(source, str):
            if path_util.path_is_url(source):
                is_url = True
                source = source.rsplit('?', 1)[0]  # Remove query string from URL, if present
            else:
                raise UsageError("Path must be a URL.")
            filename = os.path.basename(os.path.normpath(source))
        else:
            is_fileobj = True
            filename = source[0]
        return is_url, is_fileobj, filename

    def _can_unpack_file(self, path):
        return os.path.isfile(path) and self.zip_util.path_is_archive(path)

    def _unpack_file(self, source_path, dest_path, remove_source, simplify_archive):
        self.zip_util.unpack(self.zip_util.get_archive_ext(source_path), source_path, dest_path)
        if remove_source:
            path_util.remove(source_path)
        if simplify_archive:
            self._simplify_archive(dest_path)

    def _unpack_fileobj(self, source_filename, source_fileobj, dest_path, simplify_archive):
        self.zip_util.unpack(
            self.zip_util.get_archive_ext(source_filename), source_fileobj, dest_path
        )
        if simplify_archive:
            self._simplify_archive(dest_path)

    def _simplify_archive(self, path: str) -> None:
        """
        Modifies |path| in place: If |path| is a directory containing exactly
        one file / directory, then replace |path| with that file / directory.
        """
        if not os.path.isdir(path):
            return

        files = os.listdir(path)
        if len(files) == 1:
            self._simplify_directory(path, files[0])

    def _simplify_directory(self, path: str, child_path: Optional[str] = None) -> None:
        """
        Modifies |path| in place by replacing |path| with its first child file / directory.
        This method should only be called after checking to see if the |path| directory
        contains exactly one file / directory.
        """

        if child_path is None:
            child_path = os.listdir(path)[0]
        temp_path = path + crypt_util.get_random_string()
        path_util.rename(path, temp_path)
        child_path = os.path.join(temp_path, child_path)
        path_util.rename(child_path, path)
        path_util.remove(temp_path)

    def has_contents(self, bundle):
        return FileSystems.exists(self._bundle_store.get_bundle_location(bundle.uuid))

    def cleanup_existing_contents(self, bundle):
        self._bundle_store.cleanup(bundle.uuid, dry_run=False)
        bundle_update = {'data_hash': None, 'metadata': {'data_size': 0}}
        self._bundle_model.update_bundle(bundle, bundle_update)
        self._bundle_model.update_user_disk_used(bundle.owner_id)
