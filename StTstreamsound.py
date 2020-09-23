from __future__ import division

import re
import sys

from google.cloud import speech_v1 as speech
from google.cloud.speech_v1 import enums
from google.cloud.speech_v1 import types
import pyaudio
from six.moves import queue

# オーディオ録音パラメーター
RATE = 16000
CHUNK = int(RATE / 10)  # 100ms

class MicrophoneStream(object):
    """オーディオチャンクを生成するジェネレータとして録音ストリームを開きます。"""
    def __init__(self, rate, chunk):
        self._rate = rate
        self._chunk = chunk

        # オーディオデータのスレッドセーフなバッファーを作成する
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            # APIは現在、1チャネル（モノ）オーディオのみをサポートしています
            # https://goo.gl/z757pE
            channels=1, rate=self._rate,
            input=True, frames_per_buffer=self._chunk,
            # オーディオストリームを非同期に実行して、バッファーオブジェクトを埋めます。
            # これは、入力デバイスのバッファーが
            # 呼び出しスレッドがネットワーク要求を行っている間にオーバーフローする、など
            stream_callback=self._fill_buffer,
        )

        self.closed = False

        return self

    def __exit__(self, type, value, traceback):
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        # ジェネレーターにシグナルを送信して終了させ、クライアントの
        # stream_recognizeメソッドは、プロセスの終了をブロックしません。
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, frame_count, time_info, status_flags):
        """オーディオストリームからバッファにデータを継続的に収集します。"""
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        while not self.closed:
            # ブロッキングget（）を使用して、少なくとも1つのチャンクがあることを確認します
            # データ、チャンクがNoneの場合は反復を停止し、
            # オーディオストリームの終わり。
            chunk = self._buff.get()
            if chunk is None:
                return
            data = [chunk]

            # バッファリングされている他のデータを消費します。
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break

            yield b''.join(data)

def listen_print_loop(responses):
    """サーバーの応答を反復処理して出力します。

     渡される応答は、応答までブロックするジェネレータです
     サーバーによって提供されます。

     各応答には複数の結果が含まれる場合があり、各結果には
     複数の選択肢; 詳しくは、https：//goo.gl/tjCPAUをご覧ください。 ここで
     上位の結果の上位の選択肢の文字起こしのみを印刷します。

     この場合、中間結果に対しても応答が提供されます。 もし
     応答は暫定的なものであり、その最後に改行を印刷して、
     応答が最終的なものになるまで、それを上書きする次の結果。 のために
     最後に、改行を保存するために改行を印刷します。
    """
    num_chars_printed = 0
    for response in responses:
        if not response.results:
            continue

        # `results`リストは連続しています。 ストリーミングについては、
        # 最初の結果が考慮されます。これは、 `is_final`になると、
        # 次の発話の検討に移ります。
        result = response.results[0]
        if not result.alternatives:
            continue

        # 一番上の選択肢の文字起こしを表示します。
        transcript = result.alternatives[0].transcript

        # 中間結果を表示しますが、最後にキャリッジリターンがあります
        # 行なので、後続の行はそれらを上書きします。
        # 以前の結果がこれよりも長い場合は、印刷する必要があります
        # 前の結果を上書きするいくつかの余分なスペース
        overwrite_chars = ' ' * (num_chars_printed - len(transcript))

        if not result.is_final:
            sys.stdout.write(transcript + overwrite_chars + '\r')
            sys.stdout.flush()

            num_chars_printed = len(transcript)

        else:
            print(transcript + overwrite_chars)

            # 文字起こしされたフレーズのいずれかが
            # キーワードの1つ。
            if re.search(r'\b(終わり|終了)\b', transcript, re.I):
                print('Exiting..')
                break

            num_chars_printed = 0

def main():
    # 見る http://g.co/cloud/speech/docs/languages
    # サポートされている言語のリストについては、
    language_code = 'ja-JP'

    client = speech.SpeechClient()
    config = types.RecognitionConfig(
        encoding=enums.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code=language_code)
    streaming_config = types.StreamingRecognitionConfig(
        config=config,
        interim_results=True)

    with MicrophoneStream(RATE, CHUNK) as stream:
        audio_generator = stream.generator()
        requests = (types.StreamingRecognizeRequest(audio_content=content)
                    for content in audio_generator)

        responses = client.streaming_recognize(streaming_config, requests)

        # 次に、転写応答を使用します。
        listen_print_loop(responses)

if __name__ == '__main__':
    main()