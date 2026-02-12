import asyncio
import pyaudio
import fractions
import time
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame
import numpy as np

# MQTT 配置
import paho.mqtt.client as mqtt
MQTT_BROKER = "localhost"
OFFER_TOPIC = "walkie-talkie/offer"
ANSWER_TOPIC = "walkie-talkie/answer"
ICE_TOPIC = "walkie-talkie/ice"

# 音频配置
SAMPLE_RATE = 48000
CHANNELS = 1
CHUNK = 960

class WebRTCServer:
    def __init__(self):
        self.pc = RTCPeerConnection()
        self.mqtt_client = mqtt.Client(client_id="Pi_WebRTC_Server")
        
        # 获取主线程的事件循环引用
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()
            
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            print(f"连接状态: {self.pc.connectionState}")
            
        @self.pc.on("track")
        def on_track(track):
            print("收到远端音频流")
            if track.kind == "audio":
                self.player_task = asyncio.create_task(self.play_audio(track))


    async def play_audio(self, track):
        p = pyaudio.PyAudio()
        # 保持流常开以减少延迟
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
        client.subscribe([(OFFER_TOPIC, 0), (ICE_TOPIC, 0)])

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode()
        if msg.topic == OFFER_TOPIC:
            print("收到 Offer，创建 Answer...")
            # 使用 call_soon_threadsafe 安全地调度到主线程
            self.loop.call_soon_threadsafe(self.handle_offer_task, payload)
        elif msg.topic == ICE_TOPIC:
            pass

    def handle_offer_task(self, sdp):
        asyncio.ensure_future(self.handle_offer(sdp), loop=self.loop)


    async def handle_offer(self, sdp):
        offer = RTCSessionDescription(sdp=sdp, type="offer")
        await self.pc.setRemoteDescription(offer)
        
        # 创建 Answer
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        # 发送 Answer 回 C 端
        self.mqtt_client.publish(ANSWER_TOPIC, self.pc.localDescription.sdp)

if __name__ == "__main__":
    server = WebRTCServer()
    server.start_signaling()
    
    loop = asyncio.get_event_loop()
    
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("\n正在停止程序...")
    finally:
        # 清理资源
        print("清理资源中...")
        
        # 1. 停止 MQTT 客户端
        server.mqtt_client.loop_stop()
        server.mqtt_client.disconnect()
        
        # 2. 关闭 WebRTC 连接
        loop.run_until_complete(server.pc.close())
        
        # 3. 关闭事件循环
        loop.close()
        
        print("程序已退出。")
