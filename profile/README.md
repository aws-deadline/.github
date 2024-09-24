# AWS Deadline Cloud GitHub Org

Home for open source code related to AWS Deadline Cloud. AWS Deadline Cloud is a fully managed render farm service that simplifies infrastructure management and scaling for teams delivering computer generated imagery. Learn more about AWS Deadline Cloud [here](https://aws.amazon.com/deadline-cloud/).

## FAQ

- **How do I install Deadline Cloud tools on my workstation?**

    You can run the Deadline Cloud submitter installer. The installer can be found by going to the AWS Console > AWS Deadline Cloud > Downloads > Deadline Cloud submitter installer > Download.

- **Where can I find documentation for the AWS Deadline Cloud service?**
    
    See the [AWS Deadline Cloud documentation](https://docs.aws.amazon.com/deadline-cloud/).

- **How do I report a bug with an integration or tool?**

    Please open an issue in the relevant repository.

-  **How do I build my own integration?**

    You can look through [deadline-cloud-samples](https://github.com/aws-deadline/deadline-cloud-samples) for example job bundles, conda recipes, and other tooling. You can also look through the pre-built integrations below for larger examples. 

- **Where can I find information about defining jobs?**

    
    Deadline uses Open Job Description for defining jobs. See the [Open Job Description specification](https://github.com/OpenJobDescription/openjd-specifications/) or other repositories in the [Open Job Description GitHub org](https://github.com/OpenJobDescription/).

- **Do you accept feature requests or contributions?**
    
    Yes, contributions are gratefully accepted! You can open an issue to request a feature or receive early feedback on an idea before raising a pull request. Pull requests are welcomed for features that would be widely useful to the community and which align with the existing code base.

- **What if I want to adapt an existing integration to my specific workload?**
    
    Consider forking a repository and making your changes in the fork.


## Integrations
This GitHub org has integrations (such as job submission plugins) for a selection of digital content creation (DCC) software. These integrations allow users to submit jobs from inside their favorite applications. See documentation in the individual repos for more info about features, usage, and compatability.

Deadline Cloud supports service-managed fleets (SMF) and customer-managed fleets (CMF). With SMF, Deadline Cloud fully manages the workers running the jobs and includes access to some pre-packaged software applications for convenience. With CMF, the workers run on any customer-managed compute platform such as AWS EC2 or on-premise servers, and all configuration is handled by the customer. Integrations on SMF are intended to work out-of-the-box with minimal configuration; use CMF to fully customize your systems and software.

| Software | Repository | CMF| SMF | 
| ---------- | - | - | - |
| [Adobe After Effects](https://www.adobe.com/products/aftereffects.html) | [deadline-cloud-for-after-effects](https://github.com/aws-deadline/deadline-cloud-for-after-effects) | ✅ |  |
| [Autodesk 3ds Max](https://www.autodesk.com/products/3ds-max/overview) | [deadline-cloud-for-3ds-max](https://github.com/aws-deadline/deadline-cloud-for-3ds-max) | ✅ |  |
| [Autodesk Maya](https://www.autodesk.com/products/maya/overview/) | [deadline-cloud-for-maya](https://github.com/aws-deadline/deadline-cloud-for-maya) | ✅ | ✅ |
| [Blender](https://www.blender.org/) | [deadline-cloud-for-blender](https://github.com/aws-deadline/deadline-cloud-for-blender) | ✅ | ✅ |
| [KeyShot Studio](https://www.keyshot.com/) | [deadline-cloud-for-keyshot](https://github.com/aws-deadline/deadline-cloud-for-keyshot) | ✅ | ✅ |
| [Maxon Cinema 4D](https://www.maxon.net/en/cinema-4d) | [deadline-cloud-for-cinema-4d](https://github.com/aws-deadline/deadline-cloud-for-cinema-4d) | ✅ |  |
| [Nuke](https://www.foundry.com/products/nuke-family/nuke) | [deadline-cloud-for-nuke](https://github.com/aws-deadline/deadline-cloud-for-nuke) | ✅ | ✅ |
| [Rhino](https://www.rhino3d.com/) | [deadline-cloud-for-rhino](https://github.com/aws-deadline/deadline-cloud-for-rhino) | ✅ |  |
| [SideFX Houdini](https://www.sidefx.com/) | [deadline-cloud-for-houdini](https://github.com/aws-deadline/deadline-cloud-for-houdini) | ✅ | ✅ |
| [Unreal Engine](https://www.unrealengine.com/) | [deadline-cloud-for-unreal-engine](https://github.com/aws-deadline/deadline-cloud-for-unreal-engine) | ✅ |  |

## Service clients and sample repositories

| Repository | Description |
| - | - |
| [deadline-cloud](https://github.com/aws-deadline/deadline-cloud) | A Python CLI and library for interacting with Deadline Cloud, especially submitting jobs. | 
| [deadline-cloud-samples](https://github.com/aws-deadline/deadline-cloud-samples) | Example code and tools for running workloads on Deadline Cloud. | 
| [deadline-cloud-worker-agent](https://github.com/aws-deadline/deadline-cloud-worker-agent) | Software that runs on workers and communicates with the Deadline Cloud API for receiving and completing tasks. | 
