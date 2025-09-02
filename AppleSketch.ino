#include <WiFi.h>
#include <WiFiClient.h>  // Regular HTTP client

const char* ssid = "YOUR_WIFI";
const char* password = "YOUR_PASSWORD";

// Use your EC2 instance's public IP address and Flask server port (5000)
const char* serverIp = "YOUR_IP_ADDRESS";  // Replace with your EC2 public IP address
const int serverPort = 5000;  // Flask server is running on port 5000

const int in1 = 3;   // L298N IN1
const int in2 = 4;   // L298N IN2
const int ledPin = 2; // LED indicator (optional)

WiFiClient client;  // Regular HTTP client for connection

unsigned long lastTriggerTime = 0;
const unsigned long checkInterval = 5000; // 5 seconds

void setup() {
  Serial.begin(115200);

  pinMode(in1, OUTPUT);
  pinMode(in2, OUTPUT);
  pinMode(ledPin, OUTPUT);

  digitalWrite(in1, LOW);
  digitalWrite(in2, LOW);
  digitalWrite(ledPin, LOW);

  WiFi.begin(ssid, password);
  Serial.print("Connecting to Wi-Fi...");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nConnected!");
  Serial.print("IP Address: ");
  Serial.println(WiFi.localIP());  // Print IP to verify Wi-Fi is connected
}

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("Wi-Fi connected, attempting to send HTTP request...");

    // Connect to the Flask server running on EC2's public IP and port 5000
    if (client.connect(serverIp, serverPort)) {  // Specify port explicitly (5000)
      Serial.println("Connected to EC2 Flask server!");

      // Construct the GET request for your Flask endpoint
      String request = String("GET /trigger HTTP/1.1\r\n") +
                       String("Host: ") + serverIp + "\r\n" +  // Local server IP in Host header
                       String("Connection: close\r\n\r\n");

      // Send the request
      client.print(request);

      delay(500); // Wait for the response

      // Read and print the response from the server
      String response = "";
      while (client.available()) {
        String line = client.readStringUntil('\n');
        response += line;
        Serial.println(line);  // Print the response
      }

      // Check if the response is "TRIGGER"
      if (response.indexOf("TRIGGER") >= 0) {
        activateActuator(10000);  // Trigger actuator for 10 seconds (or desired duration)
      }

      client.stop(); // Close the connection
      Serial.println("Connection closed.");
    } else {
      Serial.println("Failed to connect to EC2 Flask server.");
    }
  } else {
    Serial.println("Wi-Fi disconnected. Reconnecting...");
    WiFi.reconnect();
  }

  delay(checkInterval);  // Wait before sending the next request
}


void activateActuator(int durationMs) {
  Serial.println("🎯 HIT Detected! Raising actuator...");

  digitalWrite(in1, HIGH);
  digitalWrite(in2, LOW);
  digitalWrite(ledPin, HIGH);

  delay(durationMs);

  // Retract
  Serial.println("🔽 Retracting actuator...");
  digitalWrite(in1, LOW);
  digitalWrite(in2, HIGH);
  delay(durationMs);  // Retract for the same duration

  // Stop motor
  digitalWrite(in1, LOW);
  digitalWrite(in2, LOW);
  digitalWrite(ledPin, LOW);
}
