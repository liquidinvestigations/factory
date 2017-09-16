pipeline {
    agent any
    stages {
        stage('Install Setup and Write Configuration') {
            agent any
            steps {
                sh 'git clone https://github.com/liquidinvestigations/setup.git shared/setup'
                sh '''
                echo "liquid_domain: liquid.jenkins-build.example.org" > ./shared/setup/ansible/vars/config.yml
                echo "devel: true" >> ./shared/setup/ansible/vars/config.yml
                '''
            }
        }
        stage('Build Cloud Image') {
            agent {
                label 'cloud'
            }
            steps {
                sh './prepare_cloud_image.py'
                sh './buildbot run shared/setup/bin/build_image cloud'
                sh 'qemu-img convert -f raw -O qcow2 shared/ubuntu-x86_64-raw.img shared/ubuntu-x86_64-cow2.img'
            }
            post {
                always {
                    archive 'shared/ubuntu-x86_64-raw.img'
                    archive 'shared/ubuntu-x86_64-cow2.img'
                    deleteDir()
                }
            }
        }

        stage('Build Odroid C2 Image') {
            agent {
                label 'odroid_c2'
            }
            steps {
                sh './prepare_cloud_image.py'
                sh './buildbot run shared/setup/bin/build_image odroid_c2'
            }
            post {
                always {
                    archive 'shared/ubuntu-odroid_c2-raw.img'
                    deleteDir()
                }
            }
        }
    }
}
