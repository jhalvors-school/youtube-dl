# coding: utf-8

import re
import math

from ..utils import (
    float_or_none, 
    int_or_none, 
    mimetype2ext,
    parse_codecs,
    parse_duration
)

class MPDParser:
    """
    The MPDParser class handles the job of the _parse_mpd_formats function
    from common.py in a manner which is easier to maintain. 
    """

    def __init__(self, ie, mpd_doc, mpd_id, mpd_base_url, formats_dict, mpd_url):
        self.ie = ie
        self.mpd_doc = mpd_doc
        self.mpd_id = mpd_id
        self.mpd_base_url = mpd_base_url
        self.formats_dict = formats_dict
        self.mpd_url = mpd_url
        self.namespace = ie._search_regex(
            r'(?i)^{([^}]+)?}MPD$', mpd_doc.tag, 'namespace', default=None
        )

    def _add_ns(self, path):
        return self.ie._xpath_ns(path, self.namespace)

    def is_drm_protected(self, element):
        return element.find(self._add_ns('ContentProtection')) is not None

    # As per [1, 5.3.9.2.2] SegmentList and SegmentTemplate share some
    # common attributes and elements.  We will only extract relevant
    # for us.
    def extract_common(self, source, ms_info):
        segment_timeline = source.find(self._add_ns('SegmentTimeline'))
        if segment_timeline is not None:
            s_e = segment_timeline.findall(self._add_ns('S'))
            if s_e:
                ms_info['total_number'] = 0
                ms_info['s'] = []
                for s in s_e:
                    r = int(s.get('r', 0))
                    ms_info['total_number'] += 1 + r
                    ms_info['s'].append({
                        't': int(s.get('t', 0)),
                        # @d is mandatory (see [1, 5.3.9.6.2, Table 17, page 60])
                        'd': int(s.attrib['d']),
                        'r': r,
                    })
        start_number = source.get('startNumber')
        if start_number:
            ms_info['start_number'] = int(start_number)
        timescale = source.get('timescale')
        if timescale:
            ms_info['timescale'] = int(timescale)
        segment_duration = source.get('duration')
        if segment_duration:
            ms_info['segment_duration'] = float(segment_duration)

    def extract_Initialization(self, source, ms_info):
        initialization = source.find(self._add_ns('Initialization'))
        if initialization is not None:
            ms_info['initialization_url'] = initialization.attrib['sourceURL']

    def extract_multisegment_info(self, element, ms_parent_info):
        ms_info = ms_parent_info.copy()
        segment_list = element.find(self._add_ns('SegmentList'))

        if segment_list is not None:
            self.extract_common(segment_list, ms_info)
            self.extract_Initialization(segment_list, ms_info)
            segment_urls_e = segment_list.findall(self._add_ns('SegmentURL'))
            if segment_urls_e:
                ms_info['segment_urls'] = [segment.attrib['media'] for segment in segment_urls_e]
        else:
            segment_template = element.find(self._add_ns('SegmentTemplate'))
            if segment_template is not None:
                self.extract_common(segment_template, ms_info)
                media = segment_template.get('media')
                if media:
                    ms_info['media'] = media
                initialization = segment_template.get('initialization')
                if initialization:
                    ms_info['initialization'] = initialization
                else:
                    self.extract_Initialization(segment_template, ms_info)
        return ms_info

    # Helper method for process_video_audio
    @staticmethod
    def location_key(location):
        return 'url' if re.match(r'^https?://', location) else 'path'

    # Helper method for process_video_audio
    def prepare_template(self, template_name, identifiers):
        tmpl = self.representation_ms_info[template_name]
        # First of, % characters outside $...$ templates
        # must be escaped by doubling for proper processing
        # by % operator string formatting used further (see
        # https://github.com/ytdl-org/youtube-dl/issues/16867).
        t = ''
        in_template = False
        for c in tmpl:
            t += c
            if c == '$':
                in_template = not in_template
            elif c == '%' and not in_template:
                t += c
        # Next, $...$ templates are translated to their
        # %(...) counterparts to be used with % operator
        t = t.replace('$RepresentationID$', self.representation_id)
        t = re.sub(r'\$(%s)\$' % '|'.join(identifiers), r'%(\1)d', t)
        t = re.sub(r'\$(%s)%%([^$]+)\$' % '|'.join(identifiers), r'%(\1)\2', t)
        t.replace('$$', '$')
        return t

    def parse(self):
        self.mpd_duration = parse_duration(self.mpd_doc.get('mediaPresentationDuration'))
        self.formats = []
        for period in self.mpd_doc.findall(self._add_ns('Period')):
            self.period = period
            self.period_duration = parse_duration(self.period.get('duration')) or self.mpd_duration
            self.period_ms_info = self.extract_multisegment_info(self.period, {
                'start_number': 1,
                'timescale': 1,
            })
            for adaptation_set in self.period.findall(self._add_ns('AdaptationSet')):
                self.adaptation_set = adaptation_set
                if self.is_drm_protected(adaptation_set):
                    continue
                self.adaption_set_ms_info = self.extract_multisegment_info(self.adaptation_set, self.period_ms_info)
                for representation in self.adaptation_set.findall(self._add_ns('Representation')):
                    self.representation = representation
                    if self.is_drm_protected(self.representation):
                        continue
                    self.representation_attrib = self.adaptation_set.attrib.copy()
                    self.representation_attrib.update(self.representation.attrib)
                    # According to [1, 5.3.7.2, Table 9, page 41], @mimeType is mandatory
                    self.mime_type = self.representation_attrib['mimeType']
                    self.content_type = self.mime_type.split('/')[0]
                    if self.content_type == 'text':
                        # TODO implement WebVTT downloading
                        pass
                    elif self.content_type in ('video', 'audio'):
                        self.base_url = ''
                        for element in (self.representation, self.adaptation_set, self.period, self.mpd_doc):
                            base_url_e = element.find(self._add_ns('BaseURL'))
                            if base_url_e is not None:
                                self.base_url = base_url_e.text + self.base_url
                                if re.match(r'^https?://', self.base_url):
                                    break
                        if self.mpd_base_url and not re.match(r'^https?://', self.base_url):
                            if not self.mpd_base_url.endswith('/') and not base_url.startswith('/'):
                                self.mpd_base_url += '/'
                            self.base_url = self.mpd_base_url + self.base_url
                        self.representation_id = self.representation_attrib.get('id')
                        self.lang = self.representation_attrib.get('lang')
                        self.url_el = self.representation.find(self._add_ns('BaseURL'))
                        self.filesize = int_or_none(self.url_el.attrib.get('{http://youtube.com/yt/2012/10/10}contentLength') if self.url_el is not None else None)
                        self.bandwidth = int_or_none(self.representation_attrib.get('bandwidth'))
                        self.f = {
                            'format_id': '%s-%s' % (self.mpd_id, self.representation_id) if self.mpd_id else self.representation_id,
                            'manifest_url': self.mpd_url,
                            'ext': mimetype2ext(self.mime_type),
                            'width': int_or_none(self.representation_attrib.get('width')),
                            'height': int_or_none(self.representation_attrib.get('height')),
                            'tbr': float_or_none(self.bandwidth, 1000),
                            'asr': int_or_none(self.representation_attrib.get('audioSamplingRate')),
                            'fps': int_or_none(self.representation_attrib.get('frameRate')),
                            'language': self.lang if self.lang not in ('mul', 'und', 'zxx', 'mis') else None,
                            'format_note': 'DASH %s' % self.content_type,
                            'filesize': self.filesize,
                            'container': mimetype2ext(self.mime_type) + '_dash',
                        }
                        self.f.update(parse_codecs(self.representation_attrib.get('codecs')))
                        self.representation_ms_info = self.extract_multisegment_info(self.representation, self.adaption_set_ms_info)

                        # @initialization is a regular template like @media one
                        # so it should be handled just the same way (see
                        # https://github.com/ytdl-org/youtube-dl/issues/11605)
                        if 'initialization' in self.representation_ms_info:
                            initialization_template = self.prepare_template(
                                'initialization',
                                # As per [1, 5.3.9.4.2, Table 15, page 54] $Number$ and
                                # $Time$ shall not be included for @initialization thus
                                # only $Bandwidth$ remains
                                ('Bandwidth', ))
                            self.representation_ms_info['initialization_url'] = initialization_template % {
                                'Bandwidth': self.bandwidth,
                            }

                        if 'segment_urls' not in self.representation_ms_info and 'media' in self.representation_ms_info:

                            self.media_template = self.prepare_template('media', ('Number', 'Bandwidth', 'Time'))
                            self.media_location_key = MPDParser.location_key(self.media_template)

                            # As per [1, 5.3.9.4.4, Table 16, page 55] $Number$ and $Time$
                            # can't be used at the same time
                            if '%(Number' in self.media_template and 's' not in self.representation_ms_info:
                                self.segment_duration = None
                                if 'total_number' not in self.representation_ms_info and 'segment_duration' in self.representation_ms_info:
                                    self.segment_duration = float_or_none(self.representation_ms_info['segment_duration'], self.representation_ms_info['timescale'])
                                    self.representation_ms_info['total_number'] = int(math.ceil(float(self.period_duration) / self.segment_duration))
                                self.representation_ms_info['fragments'] = [{
                                    self.media_location_key: self.media_template % {
                                        'Number': segment_number,
                                        'Bandwidth': self.bandwidth,
                                    },
                                    'duration': self.segment_duration,
                                } for segment_number in range(
                                    self.representation_ms_info['start_number'],
                                    self.representation_ms_info['total_number'] + self.representation_ms_info['start_number'])]
                            else:
                                # $Number*$ or $Time$ in media template with S list available
                                # Example $Number*$: http://www.svtplay.se/klipp/9023742/stopptid-om-bjorn-borg
                                # Example $Time$: https://play.arkena.com/embed/avp/v2/player/media/b41dda37-d8e7-4d3f-b1b5-9a9db578bdfe/1/129411
                                self.representation_ms_info['fragments'] = []
                                self.segment_time = 0
                                self.segment_d = None
                                self.segment_number = self.representation_ms_info['start_number']

                                def add_segment_url():
                                    print("add_segment_url called")
                                    segment_url = self.media_template % {
                                        'Time': self.segment_time,
                                        'Bandwidth': self.bandwidth,
                                        'Number': self.segment_number,
                                    }
                                    self.representation_ms_info['fragments'].append({
                                        self.media_location_key: segment_url,
                                        'duration': float_or_none(self.segment_d, self.representation_ms_info['timescale']),
                                    })

                                for num, s in enumerate(self.representation_ms_info['s']):
                                    self.segment_time = s.get('t') or self.segment_time
                                    self.segment_d = s['d']
                                    add_segment_url()
                                    self.segment_number += 1
                                    for r in range(s.get('r', 0)):
                                        self.segment_time += self.segment_d
                                        add_segment_url()
                                        self.segment_number += 1
                                    self.segment_time += self.segment_d
                        elif 'segment_urls' in self.representation_ms_info and 's' in self.representation_ms_info:
                            # No media template
                            # Example: https://www.youtube.com/watch?v=iXZV5uAYMJI
                            # or any YouTube dashsegments video
                            fragments = []
                            segment_index = 0
                            timescale = self.representation_ms_info['timescale']
                            for s in self.representation_ms_info['s']:
                                duration = float_or_none(s['d'], timescale)
                                for r in range(s.get('r', 0) + 1):
                                    segment_uri = self.representation_ms_info['segment_urls'][segment_index]
                                    fragments.append({
                                        location_key(segment_uri): segment_uri,
                                        'duration': duration,
                                    })
                                    segment_index += 1
                            self.representation_ms_info['fragments'] = fragments
                        elif 'segment_urls' in self.representation_ms_info:
                            # Segment URLs with no SegmentTimeline
                            # Example: https://www.seznam.cz/zpravy/clanek/cesko-zasahne-vitr-o-sile-vichrice-muze-byt-i-zivotu-nebezpecny-39091
                            # https://github.com/ytdl-org/youtube-dl/pull/14844
                            fragments = []
                            self.segment_duration = float_or_none(
                                self.representation_ms_info['segment_duration'],
                                self.representation_ms_info['timescale']) if 'segment_duration' in self.representation_ms_info else None
                            for segment_url in self.representation_ms_info['segment_urls']:
                                fragment = {
                                    location_key(segment_url): segment_url,
                                }
                                if self.segment_duration:
                                    fragment['duration'] = self.segment_duration
                                fragments.append(fragment)
                            self.representation_ms_info['fragments'] = fragments
                        # If there is a fragments key available then we correctly recognized fragmented media.
                        # Otherwise we will assume unfragmented media with direct access. Technically, such
                        # assumption is not necessarily correct since we may simply have no support for
                        # some forms of fragmented media renditions yet, but for now we'll use this fallback.
                        if 'fragments' in self.representation_ms_info:
                            self.f.update({
                                # NB: mpd_url may be empty when MPD manifest is parsed from a string
                                'url': self.mpd_url or self.base_url,
                                'fragment_base_url': self.base_url,
                                'fragments': [],
                                'protocol': 'http_dash_segments',
                            })
                            if 'initialization_url' in self.representation_ms_info:
                                initialization_url = self.representation_ms_info['initialization_url']
                                if not self.f.get('url'):
                                    self.f['url'] = initialization_url
                                self.f['fragments'].append({MPDParser.location_key(initialization_url): initialization_url})
                            self.f['fragments'].extend(self.representation_ms_info['fragments'])
                        else:
                            # Assuming direct URL to unfragmented media.
                            self.f['url'] = self.base_url

                        # According to [1, 5.3.5.2, Table 7, page 35] @id of Representation
                        # is not necessarily unique within a Period thus formats with
                        # the same `format_id` are quite possible. There are numerous examples
                        # of such manifests (see https://github.com/ytdl-org/youtube-dl/issues/15111,
                        # https://github.com/ytdl-org/youtube-dl/issues/13919)
                        full_info = self.formats_dict.get(self.representation_id, {}).copy()
                        full_info.update(self.f)
                        self.formats.append(full_info)
                    else:
                        self.ie.report_warning('Unknown MIME type %s in DASH manifest' % self.mime_type)
        return self.formats


"""




    def process_video_audio(self):
        period = self.period
        adaptation_set = self.adaptation_set
        representation = self.representation
        representation_attrib = self.representation_attrib

        mpd_doc = self.mpd_doc

        base_url = ''
        for element in (representation, adaptation_set, period, mpd_doc):
            base_url_e = element.find(self._add_ns('BaseURL'))
            if base_url_e is not None:
                base_url = base_url_e.text + base_url
                if re.match(r'^https?://', base_url):
                    break

        if self.mpd_base_url and not re.match(r'^https?://', base_url):
            if not self.mpd_base_url.endswith('/') and not base_url.startswith('/'):
                self.mpd_base_url += '/'
            base_url = self.mpd_base_url + base_url

        representation_id = representation_attrib.get('id')
        lang = representation_attrib.get('lang')
        url_el = representation.find(self._add_ns('BaseURL'))
        filesize = int_or_none(url_el.attrib.get('{http://youtube.com/yt/2012/10/10}contentLength') if url_el is not None else None)
        bandwidth = int_or_none(representation_attrib.get('bandwidth'))
        f = {
            'format_id': '%s-%s' % (self.mpd_id, representation_id) if self.mpd_id else representation_id,
            'manifest_url': self.mpd_url,
            'ext': mimetype2ext(self.mime_type),
            'width': int_or_none(representation_attrib.get('width')),
            'height': int_or_none(representation_attrib.get('height')),
            'tbr': float_or_none(bandwidth, 1000),
            'asr': int_or_none(representation_attrib.get('audioSamplingRate')),
            'fps': int_or_none(representation_attrib.get('frameRate')),
            'language': lang if lang not in ('mul', 'und', 'zxx', 'mis') else None,
            'format_note': 'DASH %s' % self.content_type,
            'filesize': filesize,
            'container': mimetype2ext(self.mime_type) + '_dash',
        }
        f.update(parse_codecs(representation_attrib.get('codecs')))
        representation_ms_info = self.extract_multisegment_info(
            representation,
            self.adaption_set_ms_info
        )

        # @initialization is a regular template like @media one
        # so it should be handled just the same way (see
        # https://github.com/ytdl-org/youtube-dl/issues/11605)
        if 'initialization' in representation_ms_info:
            initialization_template = prepare_template(
                'initialization',
                # As per [1, 5.3.9.4.2, Table 15, page 54] $Number$ and
                # $Time$ shall not be included for @initialization thus
                # only $Bandwidth$ remains
                ('Bandwidth', ), representation_ms_info, representation_id
            )
            representation_ms_info['initialization_url'] = initialization_template % {
                'Bandwidth': bandwidth,
            }

            if 'segment_urls' not in representation_ms_info and 'media' in representation_ms_info:
                media_template = prepare_template(
                    'media', ('Number', 'Bandwidth', 'Time'), 
                    representation_ms_info, representation_id
                )
                media_location_key = location_key(media_template)

                # As per [1, 5.3.9.4.4, Table 16, page 55] $Number$ and $Time$
                # can't be used at the same time
                if '%(Number' in media_template and 's' not in representation_ms_info:
                    segment_duration = None
                    if 'total_number' not in representation_ms_info and 'segment_duration' in representation_ms_info:
                        segment_duration = float_or_none(representation_ms_info['segment_duration'], representation_ms_info['timescale'])
                        representation_ms_info['total_number'] = int(math.ceil(float(period_duration) / segment_duration))
                    
                    representation_ms_info['fragments'] = [{
                        media_location_key: media_template % {
                            'Number': segment_number,
                            'Bandwidth': bandwidth,
                        },
                        'duration': segment_duration,
                    } for segment_number in range(
                        representation_ms_info['start_number'],
                        representation_ms_info['total_number'] + representation_ms_info['start_number'])]
                else:
                    # $Number*$ or $Time$ in media template with S list available
                    # Example $Number*$: http://www.svtplay.se/klipp/9023742/stopptid-om-bjorn-borg
                    # Example $Time$: https://play.arkena.com/embed/avp/v2/player/media/b41dda37-d8e7-4d3f-b1b5-9a9db578bdfe/1/129411
                    representation_ms_info['fragments'] = []
                    segment_time = 0
                    segment_d = None
                    segment_number = representation_ms_info['start_number']

                    def add_segment_url():
                        segment_url = media_template % {
                            'Time': segment_time,
                            'Bandwidth': bandwidth,
                            'Number': segment_number,
                        }
                        representation_ms_info['fragments'].append({
                        media_location_key: segment_url,
                            'duration': float_or_none(segment_d, representation_ms_info['timescale']),
                        })

                    for num, s in enumerate(representation_ms_info['s']):
                        segment_time = s.get('t') or segment_time
                        segment_d = s['d']
                        add_segment_url()
                        segment_number += 1
                        for r in range(s.get('r', 0)):
                            segment_time += segment_d
                            add_segment_url()
                            segment_number += 1
                        segment_time += segment_d
            elif 'segment_urls' in representation_ms_info and 's' in representation_ms_info:
                # No media template
                # Example: https://www.youtube.com/watch?v=iXZV5uAYMJI
                # or any YouTube dashsegments video
                fragments = []
                segment_index = 0
                timescale = representation_ms_info['timescale']
                for s in representation_ms_info['s']:
                    duration = float_or_none(s['d'], timescale)
                    for r in range(s.get('r', 0) + 1):
                        segment_uri = representation_ms_info['segment_urls'][segment_index]
                        fragments.append({
                            location_key(segment_uri): segment_uri,
                            'duration': duration,
                        })
                        segment_index += 1
                representation_ms_info['fragments'] = fragments
            elif 'segment_urls' in representation_ms_info:
                # Segment URLs with no SegmentTimeline
                # Example: https://www.seznam.cz/zpravy/clanek/cesko-zasahne-vitr-o-sile-vichrice-muze-byt-i-zivotu-nebezpecny-39091
                # https://github.com/ytdl-org/youtube-dl/pull/14844
                fragments = []
                segment_duration = float_or_none(
                    representation_ms_info['segment_duration'],
                    representation_ms_info['timescale']) if 'segment_duration' in representation_ms_info else None
                for segment_url in representation_ms_info['segment_urls']:
                    fragment = {
                        location_key(segment_url): segment_url,
                    }
                    if segment_duration:
                        fragment['duration'] = segment_duration
                    fragments.append(fragment)
                representation_ms_info['fragments'] = fragments
            # If there is a fragments key available then we correctly recognized fragmented media.
            # Otherwise we will assume unfragmented media with direct access. Technically, such
            # assumption is not necessarily correct since we may simply have no support for
            # some forms of fragmented media renditions yet, but for now we'll use this fallback.
            if 'fragments' in representation_ms_info:
                f.update({
                    # NB: mpd_url may be empty when MPD manifest is parsed from a string
                    'url': mpd_url or base_url,
                    'fragment_base_url': base_url,
                    'fragments': [],
                    'protocol': 'http_dash_segments',
                })
                if 'initialization_url' in representation_ms_info:
                    initialization_url = representation_ms_info['initialization_url']
                    if not f.get('url'):
                        f['url'] = initialization_url
                    f['fragments'].append({location_key(initialization_url): initialization_url})
                f['fragments'].extend(representation_ms_info['fragments'])
            else:
                # Assuming direct URL to unfragmented media.
                f['url'] = base_url

                # According to [1, 5.3.5.2, Table 7, page 35] @id of Representation
                # is not necessarily unique within a Period thus formats with
                # the same `format_id` are quite possible. There are numerous examples
                # of such manifests (see https://github.com/ytdl-org/youtube-dl/issues/15111,
                # https://github.com/ytdl-org/youtube-dl/issues/13919)
                full_info = self.formats_dict.get(representation_id, {}).copy()
                full_info.update(f)
                self.formats.append(full_info)

    def process_representation(self, representation):
        if self.is_drm_protected(representation):
            return

        representation_attrib = self.adaptation_set.attrib.copy()
        representation_attrib.update(representation.attrib)

        # According to [1, 5.3.7.2, Table 9, page 41], @mimeType is mandatory
        mime_type = representation_attrib['mimeType']
        content_type = mime_type.split('/')[0]

        self.representation = representation
        self.representation_attrib = representation_attrib
        self.mime_type = mime_type
        self.content_type = content_type

        if content_type == 'text':
            # TODO implement WebVTT downloading
            return
        elif content_type in ('video', 'audio'):
            self.process_video_audio()
        else:
            self.ie.report_warning('Unknown MIME type %s in DASH manifest' % mime_type)
        

    def process_adaptation_set(self, adaptation_set):
        if self.is_drm_protected(adaptation_set):
            return

        self.adaptation_set = adaptation_set
        self.adaption_set_ms_info = self.extract_multisegment_info(
            adaptation_set,
            self.period_ms_info
        )
        for representation in adaptation_set.findall(self._add_ns('Representation')):
            self.process_representation(representation)

    def process_period(self, period):
        self.period = period
        self.period_duration = parse_duration(period.get('duration')) or self.mpd_duration
        self.period_ms_info = self.extract_multisegment_info(period, {
            'start_number': 1,
            'timescale': 1,
        })
        for adaptation_set in period.findall(self._add_ns('AdaptationSet')):
            self.process_adaptation_set(adaptation_set)

    def parse(self):
        self.formats = []
        self.mpd_duration = parse_duration(self.mpd_doc.get('mediaPresentationDuration'))
        for period in self.mpd_doc.findall(self._add_ns('Period')):
            self.process_period(period)
        return self.formats
"""