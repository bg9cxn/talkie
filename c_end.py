import asyncio
import pyaudio
import fractions
import time
import threading
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame
import numpy as np

# MQTT 配置
import paho.mqtt.client as mqtt
MQTT_BROKER = "192.168.3.24"
OFFER_TOPIC = "walkie-talkie/offer"
ANSWER_TOPIC = "walkie-talkie/answer"
ICE_TOPIC = "walkie-talkie/ice"

# 音频配置
SAMPLE_RATE = 48000
CHANNELS = 1
CHUNK = 960  # 20ms @ 48kHz

class AudioInputTrack(MediaStreamTrack):
    kind = "audio"
    
    def __init__(self):
        super().__init__()
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(format=pyaudio.paInt16,
                                 channels=CHANNELS,
                                 rate=SAMPLE_RATE,
                                 input=True,
                                 frames_per_buffer=CHUNK)
        self.lock = threading.Lock()
        self.is_active = False # 模拟 PTT 开关

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        with self.lock:
            if not self.is_active:
                # 如果 PTT 未按下，返回静音帧
                frame = AudioFrame(format='s16', layout='mono', samples=CHUNK)
                for plane in frame.planes:
                    plane.update(bytes(CHUNK * 2)) # 静音
                frame.pts = pts
                frame.time_base = time_base
                return frame

            # 读取真实音频
            data = self.stream.read(CHUNK, exception_on_overflow=False)
            # 转换为 numpy array 再转回 bytes 确保格式正确
            array = np.frombuffer(data, dtype=np.int16)
            
            # 构建 AudioFrame (aiortc 内部会自动使用 Opus 编码)
            frame = AudioFrame(format='s16', layout='mono', samples=CHUNK)
            for plane in frame.planes:
                plane.update(array.tobytes())
            frame.pts = pts
            frame.time_base = time_base
            return frame

    def set_ptt(self, active):
        with self.lock:
            self.is_active = active

    def stop(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.p.terminate()

class WebRTCClient:
    def __init__(self):
        self.pc = RTCPeerConnection()
        self.mqtt_client = mqtt.Client(client_id="Windows_WebRTC_Client")
        self.audio_track = AudioInputTrack()
        
        # 获取主线程的事件循环引用
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()
            
        # WebRTC 事件处理
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            print(f"连接状态: {self.pc.connectionState}")
        
        @self.pc.on("track")
        def on_track(track):
            print("收到远端音频流")
            if track.kind == "audio":
                self.player_task = asyncio.create_task(self.play_audio(track))

        # 添加本地音频轨道
        self.pc.addTrack(self.audio_track)

    async def play_audio(self, track):
        p = pyaudio.PyAudio()
        stream = p.open(format=pyaudio.paInt16,
                        channels=CHANNELS,
                        rate=SAMPLE_RATE,
                        output=True)
        
        while True:
            try:
                frame = await track.recv()
                stream.write(frame.planes[0].to_bytes())
            except Exception as e:
                print(f"播放错误: {e}")
                break
        
        stream.stop_stream()
        stream.close()
        p.terminate()

    def start_signaling(self):
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.connect(MQTT_BROKER, 1883, 60)
        self.mqtt_client.loop_start()

    def on_connect(self, client, userdata, flags, rc):
        print(f"MQTT 连接成功: {rc}")
        self.loop.call_soon_threadsafe(self.create_offer_task)

    def create_offer_task(self):
        asyncio.ensure_future(self.create_offer(), loop=self.loop)

    async def create_offer(self):
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self.mqtt_client.publish(OFFER_TOPIC, self.pc.localDescription.sdp)
        
    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode()
        if msg.topic == ANSWER_TOPIC:
            self.loop.call_soon_threadsafe(self.handle_answer_task, payload)
        elif msg.topic == ICE_TOPIC:
            pass

    def handle_answer_task(self, sdp):
        asyncio.ensure_future(self.handle_answer(sdp), loop=self.loop)

    async def handle_answer(self, sdp):
        answer = RTCSessionDescription(sdp=sdp, type="answer")
        await self.pc.setRemoteDescription(answer)

    def set_ptt(self, active):
        self.audio_track.set_ptt(active)


if __name__ == "__main__":
    import keyboard
    
    client = WebRTCClient()
    client.start_signaling()
    
    print("按住 '空格' 键说话，松开停止...")
    
    # 运行事件循环
    loop = asyncio.get_event_loop()
    
    # 定义一个后台线程来监听键盘，避免阻塞主循环
    def keyboard_listener():
        try:
            while True:
                if keyboard.is_pressed('space'):
                    client.set_ptt(True)
                else:
                    client.set_ptt(False)
                time.sleep(0.01)
        except KeyboardInterrupt:
            pass
    
    # 启动键盘监听线程
    listener_thread = threading.Thread(target=keyboard_listener)
    listener_thread.daemon = True
    listener_thread.start()
    
    try:
        # 主线程持续运行事件循环，处理网络和音频 IO
        loop.run_forever()
    except KeyboardInterrupt:
        print("\n正在停止程序...")
    finally:
        # 清理资源
        print("清理资源中...")
        
        # 1. 停止 MQTT 客户端
        client.mqtt_client.loop_stop()
        client.mqtt_client.disconnect()
        
        # 2. 关闭 WebRTC 连接 (修改此处)
        # 使用当前已有的 loop，而不是创建新的 loop
        loop.run_until_complete(client.pc.close())
        
        # 3. 停止音频采集
        client.audio_track.stop()
        
        # 4. 关闭事件循环
        loop.close()
        
        print("程序已退出。")
