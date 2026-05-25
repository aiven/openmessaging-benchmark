PACKAGE_TAR := package/target/openmessaging-benchmark-0.0.1-SNAPSHOT-bin.tar.gz

.PHONY: build clean

build: $(PACKAGE_TAR)

$(PACKAGE_TAR):
	./mvnw clean package -Dlicense.skip=true -DskipTests

clean:
	./mvnw clean
