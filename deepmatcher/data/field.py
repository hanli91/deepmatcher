import logging
import os
import tarfile
import zipfile

import six

import fastText
import torch
from torchtext import data, vocab
from torchtext.utils import download_from_url

logger = logging.getLogger(__name__)


class FastText(vocab.Vectors):

    def __init__(self,
                 suffix='wiki-news-300d-1M.vec.zip',
                 url_base='https://s3-us-west-1.amazonaws.com/fasttext-vectors/',
                 **kwargs):
        url = url_base + suffix
        base, ext = os.path.splitext(suffix)
        name = suffix if ext == '.vec' else base
        super(FastText, self).__init__(name, url=url, **kwargs)


class FastTextBinary(vocab.Vectors):

    name_base = 'wiki.{}.bin'
    _direct_en_url = 'https://drive.google.com/uc?export=download&id=1Vih8gAmgBnuYDxfblbT94P6WjB7s1ZSh'

    def __init__(self, language='en', url_base=None, cache=None):
        """
        Arguments:
           language: Language of fastText pre-trained embedding model
           cache: directory for cached model
         """
        cache = os.path.expanduser(cache)
        if language == 'en' and url_base is None:
            url = FastTextBinary._direct_en_url
            self.destination = os.path.join(cache, 'wiki.' + language + '.bin')
        else:
            if url_base is None:
                url_base = 'https://s3-us-west-1.amazonaws.com/fasttext-vectors/wiki.{}.zip'
            url = url_base.format(language)
            self.destination = os.path.join(cache, 'wiki.' + language + '.zip')
        name = FastTextBinary.name_base.format(language)

        self.cache(name, cache, url=url)

    def __getitem__(self, token):
        return torch.Tensor(self.model.get_word_vector(token))

    def cache(self, name, cache, url=None):
        path = os.path.join(cache, name)
        if not os.path.isfile(path) and url:
            logger.info('Downloading vectors from {}'.format(url))
            if not os.path.exists(cache):
                os.makedirs(cache)
            if not os.path.isfile(self.destination):
                download_from_url(url, self.destination)
            logger.info('Extracting vectors into {}'.format(cache))
            ext = os.path.splitext(self.destination)[1][1:]
            if ext == 'zip':
                with zipfile.ZipFile(self.destination, "r") as zf:
                    zf.extractall(cache)
            elif ext == 'gz':
                with tarfile.open(self.destination, 'r:gz') as tar:
                    tar.extractall(path=cache)
        if not os.path.isfile(path):
            raise RuntimeError('no vectors found at {}'.format(path))

        self.model = fastText.load_model(path)
        self.dim = len(self['a'])


class MatchingField(data.Field):
    _cached_vec_data = {}

    def __init__(self, tokenize='moses', id=False, **kwargs):
        self.tokenizer_arg = tokenize
        self.is_id = id
        super(MatchingField, self).__init__(tokenize=tokenize, **kwargs)

    def preprocess_args(self):
        attrs = [
            'sequential', 'init_token', 'eos_token', 'unk_token', 'preprocessing',
            'lower', 'tokenizer_arg'
        ]
        args_dict = {attr: getattr(self, attr) for attr in attrs}
        for param, arg in list(six.iteritems(args_dict)):
            if six.callable(arg):
                del args_dict[param]
        return args_dict

    @classmethod
    def _get_vector_data(cls, vecs, cache):
        if not isinstance(vecs, list):
            vecs = [vecs]

        vec_datas = []
        for vec in vecs:
            if not isinstance(vec, vocab.Vectors):
                vec_name = vec
                vec_data = cls._cached_vec_data.get(vec_name)
                if vec_data is None:
                    parts = vec_name.split('.')
                    if parts[0] == 'fasttext':
                        if parts[2] == 'bin':
                            vec_data = FastTextBinary(language=parts[1], cache=cache)
                        elif parts[2] == 'vec' and parts[1] == 'wiki':
                            vec_data = FastText(
                                suffix='wiki-news-300d-1M.vec.zip', cache=cache)
                        elif parts[2] == 'vec' and parts[1] == 'crawl':
                            vec_data = FastText(
                                suffix='crawl-300d-2M.vec.zip', cache=cache)
                if vec_data is None:
                    vec_data = vocab.pretrained_aliases[vec_name](cache=cache)
                cls._cached_vec_data[vec_name] = vec_data
                vec_datas.append(vec_data)
            else:
                vec_datas.append(vec)

        return vec_datas

    def build_vocab(self, *args, vectors=None, cache=None, **kwargs):
        if cache is not None:
            cache = os.path.expanduser(cache)
        if vectors is not None:
            vectors = MatchingField._get_vector_data(vectors, cache)
        super(MatchingField, self).build_vocab(*args, vectors=vectors, **kwargs)

    def numericalize(self, arr, *args, **kwargs):
        if not self.is_id:
            return super(MatchingField, self).numericalize(arr, *args, **kwargs)
        return arr
